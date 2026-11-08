"""Lifecycle-owned terminal retention and incremental reads of primary facts.

No decisions live here. Review facts remain in review_admission's ledgers;
terminal facts survive replacement of the single current runner record.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

TERMINALS = ".runner/terminal-runs.jsonl"
MAX_LINE = 2_000_000
PAGE_SIZE = 32


def owner_turn() -> str | None:
    """Optional caller correlation; not authorization to launch or accept work."""
    value = os.environ.get("TASK_AGENT_OWNER_TURN", "")
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None


def run_identity(meta: dict) -> str | None:
    if not meta.get("started_at") or meta.get("dry_run"):
        return None
    return str(meta.get("run_id") or hashlib.sha256(json.dumps(
        [meta.get("task_dir"), meta["started_at"], meta.get("runner"),
         meta.get("workflow")], sort_keys=True).encode()).hexdigest())


def current_fact(task_dir: Path, meta: dict) -> dict | None:
    """Project a recorded end, or proven loss of both supervisors, honestly.

    No exit status or death time can be recovered from a missing process.
    Unknown identity/visibility is not negative liveness evidence.
    """
    identity = run_identity(meta)
    if not identity:
        return None
    record = {"schema_version": 1, "event_id": identity, "run_id": identity, "kind": "run_terminal",
              **{key: meta.get(key) for key in (
                  "started_at", "finished_at", "runner", "workflow", "outcome",
                  "exit_code", "review_kind", "child_started_at", "owner_turn")},
              "role": meta.get("review_admission", {}).get("access_profile", {}).get("role")}
    record["grants_write"] = meta.get("review_admission", {}).get("access_profile", {}).get("grants_write")
    if meta.get("finished_at") and meta.get("outcome"):
        return record
    try:
        from . import task_runner, task_contract
    except ImportError:
        import task_runner, task_contract
    if (not meta.get("child_started_at") or not task_runner.process_identity_available()
            or not task_runner.runner_pid_namespace_visible(meta)):
        return None
    for pid_key, identity_key in (("pid", "process_identity"),
                                  ("watcher_pid", "watcher_process_identity")):
        if (not isinstance(meta.get(pid_key), int) or not meta.get(identity_key)
                or task_runner.process_is_recorded_instance(meta[pid_key], meta[identity_key])[0]):
            return None
        if task_runner.process_identity(meta[pid_key]) is None and task_runner.pid_is_running(meta[pid_key]):
            return None  # inaccessible identity is not proof that the run died
    record.update(event_id=identity + ":supervision_lost", kind="run_supervision_lost",
                  outcome="supervision_lost", finished_at=None, exit_code=None,
                  liveness={key: meta[key] for key in
                            ("pid", "process_identity", "watcher_pid", "watcher_process_identity")})
    # This is primary, unfinalized reviewer evidence, not a round or acceptance.
    if (meta.get("workflow") == "standard" and meta.get("review_kind") == "technical"
            and record["role"] == "reviewer"):
        verdict = task_contract.published_review_verdict(task_dir)
        if verdict:
            record["published_verdict"] = verdict
            record["verdict_source"] = str(task_dir / "findings.md")
            record["verdict_sha256"] = hashlib.sha256((task_dir / "findings.md").read_bytes()).hexdigest()
    return record


def retain_terminal(task_dir: Path, meta: dict) -> None:
    """Called before terminal publication and before a successor replaces it.

    The run identity, not a scope or PID, covers read-only and failed launches.
    Repeated publication reads only the bounded last record. fsync precedes
    replacement, so a crash cannot erase the terminal fact it supersedes.
    """
    record = current_fact(task_dir, meta)
    if record is None:
        return
    identity = record["event_id"]
    encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
    if len(encoded) > 8192:
        raise ValueError("terminal fact exceeds 8192 bytes")
    path = task_dir / TERMINALS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - 16384))
        tail = handle.read()
        # Discard only an unfinished append owned by this writer; complete facts
        # are immutable. A retry then publishes the same run once.
        end = tail.rfind(b"\n") + 1
        if end < len(tail):
            handle.truncate(size - len(tail) + end)
        lines = tail[:end].splitlines()
        if lines and json.loads(lines[-1]).get("event_id") == identity:
            return
        handle.seek(0, os.SEEK_END)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _anchor(handle, offset: int) -> str:
    handle.seek(max(0, offset - 256))
    return hashlib.sha256(handle.read(min(offset, 256))).hexdigest()


def _event_id(value: dict) -> str:
    return str(value.get("event_id") or hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()).hexdigest())


def read_page(path: Path, cursor: dict | None, *, baseline: bool) -> dict:
    """Byte cursor plus a content anchor; replacement is never decided by mtime.

    Only recovery after a changed prefix scans history. A copied prefix locates
    the prior event identity; a fresh rotated generation starts at its beginning.
    Incomplete appends stay beyond the cursor. Missing observed files refuse.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        if cursor and cursor.get("offset", 0):
            raise ValueError(f"observed fact ledger disappeared: {path}")
        return {"cursor": {"offset": 0}, "events": [], "more": False}
    with handle:
        size = handle.seek(0, os.SEEK_END)
        offset = int((cursor or {}).get("offset", 0))
        last_id = (cursor or {}).get("last_id")
        if baseline:
            # Seek only the bounded tail; do not read historical rounds at startup.
            width = min(size, 8192)
            while True:
                handle.seek(size - width)
                tail = handle.read(width)
                if tail.count(b"\n") >= 2 or width == size:
                    break
                if width >= MAX_LINE:
                    raise ValueError(f"fact line exceeds read bound: {path}")
                width = min(size, MAX_LINE, width * 2)
            end = tail.rfind(b"\n") + 1
            if size and not end:
                raise ValueError(f"fact line exceeds read bound: {path}")
            offset = size - len(tail) + end
            lines = tail[:end].splitlines()
            last_id = _event_id(json.loads(lines[-1])) if lines else None
        elif offset and (offset > size or _anchor(handle, offset) != cursor.get("anchor")):
            # Ledgers do not rotate in the engine. If an operator replaces one,
            # content identity recovers a copied prefix or reads the new generation.
            handle.seek(0)
            recovered = 0
            while True:
                line = handle.readline(MAX_LINE + 1)
                if len(line) > MAX_LINE:
                    raise ValueError(f"fact line exceeds read bound: {path}")
                if not line or not line.endswith(b"\n"):
                    break
                if _event_id(json.loads(line)) == last_id:
                    recovered = handle.tell()
                    break
            offset = recovered
        handle.seek(offset)
        events = []
        if not baseline:
            for _ in range(PAGE_SIZE):
                start = handle.tell()
                line = handle.readline(MAX_LINE + 1)
                if len(line) > MAX_LINE:
                    raise ValueError(f"fact line exceeds read bound: {path}")
                if not line or not line.endswith(b"\n"):
                    break
                value = json.loads(line)
                last_id = _event_id(value)
                offset = handle.tell()
                events.append({"id": last_id, "fact": value,
                               "source": f"{path}#bytes={start}:{offset}",
                               "sha256": hashlib.sha256(line).hexdigest()})
        return {"events": events, "more": offset < size,
                "cursor": {"offset": offset, "anchor": _anchor(handle, offset),
                           "last_id": last_id}}


def facts(task_dir: Path, cursors: dict | None = None, *, baseline: bool = False) -> dict:
    # Imported lazily to keep retention independent of admission/runner imports.
    try:
        from . import review_admission
    except ImportError:
        import review_admission
    streams = {"runs": TERMINALS, "reviews": review_admission.ROUNDS_LEDGER,
               "refusals": review_admission.OBLIGATIONS_LEDGER,
               "admissions": review_admission.ADMISSIONS_LEDGER}
    cursors = cursors or {}
    pages = {key: read_page(task_dir / name, cursors.get(key), baseline=baseline)
             for key, name in streams.items()}
    path = task_dir / ".runner/runner.json"
    raw = path.read_bytes() if path.exists() else b"{}"
    meta = json.loads(raw)
    current = current_fact(task_dir, meta)
    known = cursors.get("legacy_terminal")
    direct = set(cursors.get("direct_pending", []))
    seen = cursors.get("current_seen", known)
    if baseline:
        known = current["event_id"] if current else None
        seen = known
    archived = {row["id"] for row in pages["runs"]["events"]}
    fields = ("event_id", "run_id", "kind", "round", "decision", "recorded_at", "review_attempt",
              "review_provider", "started_at", "finished_at", "runner", "workflow",
              "outcome", "exit_code", "review_kind", "child_started_at", "source",
              "owner_turn", "role", "grants_write", "liveness", "published_verdict", "verdict_source", "verdict_sha256")
    events = [{**row, "stream": key,
               "fact": {field: row["fact"][field] for field in fields if field in row["fact"]}}
              for key, page in pages.items()
              for row in page["events"]
              if (key != "runs" or (row["id"] != known and row["id"] not in direct))
              and (key != "admissions" or row["fact"].get("decision") == "refused")]
    if (not baseline and current and current["event_id"] not in {known, seen}
            and current["event_id"] not in direct and current["event_id"] not in archived
            and (not pages["runs"]["more"] or not pages["runs"]["events"])):
        # Older running watchers do not know the archive. Read their current
        # terminal too, without changing their metadata or manufacturing a round.
        events.append({"id": current["event_id"], "stream": "runs", "fact": current,
                       "source": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
        direct.add(current["event_id"])
    if current and (baseline or current["event_id"] in archived or current["event_id"] in direct):
        seen = current["event_id"]
    direct.difference_update(archived)
    run = {"id": run_identity(meta), "child_started_at": meta.get("child_started_at"),
           "owner_turn": meta.get("owner_turn"),
           "role": meta.get("review_admission", {}).get("access_profile", {}).get("role"),
           "grants_write": meta.get("review_admission", {}).get("access_profile", {}).get("grants_write"),
           "review_kind": meta.get("review_kind")}
    return {"schema_version": 1, "events": events, "run_id": run_identity(meta),
            "run_started": bool(meta.get("child_started_at")),
            "run": run,
            "cursors": {**{key: page["cursor"] for key, page in pages.items()},
                        "legacy_terminal": known, "current_seen": seen,
                        "direct_pending": sorted(direct)},
            "more": any(page["more"] for page in pages.values())}
