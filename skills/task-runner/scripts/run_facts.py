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


def run_identity(meta: dict) -> str | None:
    if not meta.get("started_at") or meta.get("dry_run"):
        return None
    return str(meta.get("run_id") or hashlib.sha256(json.dumps(
        [meta.get("task_dir"), meta["started_at"], meta.get("runner"),
         meta.get("workflow")], sort_keys=True).encode()).hexdigest())


def retain_terminal(task_dir: Path, meta: dict) -> None:
    """Called before terminal publication and before a successor replaces it.

    The run identity, not a scope or PID, covers read-only and failed launches.
    Repeated publication reads only the bounded last record. fsync precedes
    replacement, so a crash cannot erase the terminal fact it supersedes.
    """
    identity = run_identity(meta)
    if not identity or not meta.get("finished_at") or not meta.get("outcome"):
        return
    record = {"schema_version": 1, "event_id": identity, "kind": "run_terminal",
              **{key: meta.get(key) for key in (
                  "started_at", "finished_at", "runner", "workflow", "outcome",
                  "exit_code", "review_kind", "child_started_at")}}
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
    meta = json.loads(path.read_text()) if path.exists() else {}
    known = cursors.get("legacy_terminal")
    if baseline:
        known = run_identity(meta) if meta.get("finished_at") else None
    fields = ("event_id", "kind", "round", "decision", "recorded_at", "review_attempt",
              "review_provider", "started_at", "finished_at", "runner", "workflow",
              "outcome", "exit_code", "review_kind", "child_started_at", "source")
    events = [{**row, "stream": key,
               "fact": {field: row["fact"][field] for field in fields if field in row["fact"]}}
              for key, page in pages.items()
              for row in page["events"]
              if (key != "runs" or row["id"] != known)
              and (key != "admissions" or row["fact"].get("decision") == "refused")]
    return {"schema_version": 1, "events": events, "run_id": run_identity(meta),
            "run_started": bool(meta.get("child_started_at")),
            "cursors": {**{key: page["cursor"] for key, page in pages.items()},
                        "legacy_terminal": known},
            "more": any(page["more"] for page in pages.values())}
