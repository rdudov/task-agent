#!/usr/bin/env python3
"""The one callable interface for creating a task and for every task-metadata write.

The task directory is the source of truth. `.state/tasks-index.db` holds one
table, rebuilt from `tasks/` whenever it is missing or behind, and losing it
costs nothing but the time to walk the tree again -- including the next task
number, which is `MAX(id) + 1` over that table after it has been synced.

Everything hangs off one operation, `discover_and_sync`. `query`, `add`,
`reindex`, the write commands and `check` all call it, and none of them has a
second opinion about what is on disk. That is the whole design: an `add` that
allocated from a table it had not synced would hand out a number an existing
directory already owns.

Reading and writing frontmatter are deliberately asymmetric. Reads go through
PyYAML; writes replace only the lines of the key being changed and are parsed
back before they are stored, because re-serializing would rewrite the quoting,
key order and comments of every task.md at once.

Walking `tasks/` reads only `task.md` in each directory. Task payloads are
several gigabytes and must never be scanned.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import yaml

# TASKS_INDEX_ROOT lets tests point the real script at a throwaway tree instead
# of forcing them to burn task numbers in tasks/. An installed engine delegates
# workspace resolution to the runner's existing owner, while direct repository
# use retains the source-tree default.
def repo_root() -> Path:
    configured = os.environ.get("TASKS_INDEX_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    source_file = Path(__file__).resolve()
    source_root = source_file.parents[3]
    if source_file == source_root / "skills/task-creator/scripts/tasks_index.py":
        configured = os.environ.get("TASK_AGENT_ROOT")
        if configured:
            return Path(configured).expanduser().resolve()
        return source_root
    from task_agent.task_runner import repo_root as runner_repo_root

    return runner_repo_root()


REPO_ROOT = repo_root()
TASKS_DIR = REPO_ROOT / "tasks"
DB_PATH = REPO_ROOT / ".state" / "tasks-index.db"

SCHEMA_VERSION = 2
CANONICAL_STATUSES = ("planned", "in_progress", "blocked", "completed", "cancelled")
ACTIVE_STATUSES = ("planned", "in_progress", "blocked")
# Carried by a task whose frontmatter cannot be read. Deliberately outside
# CANONICAL_STATUSES so it can never be mistaken for a real state, and
# deliberately still stored: a task on disk must never vanish from lookup.
UNKNOWN_STATUS = "unknown"

PREFIX_RE = re.compile(r"^(\d+)-")
# `2026-05-26-openclaw-...` matches PREFIX_RE with group 1 == "2026", so a task
# whose frontmatter happened to say `id: 2026` would agree with its own prefix.
DATE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-|$)")
LIST_KEYS = ("projects", "trips")
BUSY_TIMEOUT_MS = int(os.environ.get("TASKS_INDEX_BUSY_TIMEOUT_MS", "30000"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    path          TEXT PRIMARY KEY,
    id            INTEGER UNIQUE,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,
    date          TEXT,
    status        TEXT NOT NULL,
    status_detail TEXT,
    request_id    TEXT UNIQUE,
    projects_json TEXT NOT NULL DEFAULT '[]',
    trips_json    TEXT NOT NULL DEFAULT '[]',
    task_mtime_ns INTEGER NOT NULL,
    task_size     INTEGER NOT NULL,
    indexed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_by_id ON tasks(id);
CREATE INDEX IF NOT EXISTS tasks_by_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_by_date ON tasks(date);
"""
REQUIRED_COLUMNS = frozenset({
    "path", "id", "slug", "title", "date", "status", "status_detail", "request_id",
    "projects_json", "trips_json", "task_mtime_ns", "task_size", "indexed_at",
})

TASK_TEMPLATE = """# {title}

## Summary
{summary}

## Inputs
- none captured yet

## Open Questions
- none

## Status
- none

## Parent Task
none

## Related Tasks
- none

## Projects
{projects}

## Trips
{trips}
"""

PLAN_TEMPLATE = """# Plan

## Goal
{summary}

## Steps
1. Understand the current context.
2. Implement the required change.
3. Verify the result.
"""


class TaskIndexError(Exception):
    """A user-facing error; printed without a traceback."""


# --- reading the task tree ---------------------------------------------------

def read_frontmatter(text: str) -> tuple[dict, str | None]:
    if not text.startswith("---"):
        return {}, "no YAML frontmatter"
    end = text.find("\n---", 3)
    if end < 0:
        return {}, "unterminated YAML frontmatter"
    try:
        fields = yaml.safe_load(text[3:end])
    except yaml.YAMLError as error:
        return {}, f"unreadable YAML frontmatter: {str(error).splitlines()[0]}"
    if fields is None:
        return {}, None
    if not isinstance(fields, dict):
        return {}, "YAML frontmatter is not a mapping"
    for key in LIST_KEYS:
        if key in fields and not isinstance(fields[key], (list, type(None))):
            return fields, f"`{key}` must be a list"
    return fields, None


def _links(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def read_record(task_dir: Path) -> dict:
    """Index row for one task directory. Reads only task.md and never raises.

    `id` comes from the frontmatter and from nowhere else. Deriving it from the
    directory name is what let a `2026-05-26-...` archive directory present
    itself as task 2026 and pull the allocator up with it; a directory whose
    frontmatter has no usable id gets `None` here, stays visible, and is named
    by `check` rather than feeding `MAX(id)`.
    """
    try:
        text = (task_dir / "task.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    fields, error = read_frontmatter(text)

    value = fields.get("id")
    # `id: yes` is a YAML boolean and `isinstance(True, int)` is true, so a bare
    # bool would otherwise be indexed as the number 1.
    task_id = value if isinstance(value, int) and not isinstance(value, bool) else None

    title = fields.get("title")
    if not isinstance(title, str) or not title.strip():
        title = _first_heading(text) or task_dir.name

    # `date: 2026-07-01` is a YAML timestamp, not a string, and every row here
    # carries one. The index column is text and callers compare it as text.
    date = fields.get("date")
    if isinstance(date, _dt.date):
        date = date.isoformat()

    status = fields.get("status")
    detail = fields.get("status_detail")
    return {
        "path": f"tasks/{task_dir.name}",
        "id": task_id,
        "slug": task_dir.name,
        "title": title,
        "date": date if isinstance(date, str) else None,
        "status": status if status in CANONICAL_STATUSES else UNKNOWN_STATUS,
        "status_detail": detail if isinstance(detail, str) and detail else None,
        "request_id": (
            fields.get("request_id")
            if isinstance(fields.get("request_id"), str) and fields.get("request_id").strip()
            else None
        ),
        "projects": _links(fields.get("projects")),
        "trips": _links(fields.get("trips")),
        "_fields": fields,
        "_error": error,
    }


def iter_task_dirs() -> list[Path]:
    """Immediate task directories. Hidden names are staging, not tasks."""
    if not TASKS_DIR.is_dir():
        return []
    return sorted((p for p in TASKS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: p.name)


# --- the database ------------------------------------------------------------

def schema_is_current(conn: sqlite3.Connection) -> bool:
    """Every required column and the UNIQUE identity, not a sample of three.

    Checking `path`, `id` and `task_mtime_ns` left a version-1 database missing
    any other column trusted, so it failed later in ordinary SQL instead of
    being discarded here. The table is explicitly rebuildable from `tasks/`, so
    discarding it is the cheap answer and being thorough costs nothing.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if not columns:
        return True  # no table yet; SCHEMA is about to create it
    if not REQUIRED_COLUMNS <= columns:
        return False
    # Without UNIQUE(id) a repeated number reaches the table instead of `issues`.
    for index in conn.execute("PRAGMA index_list(tasks)"):
        if not index["unique"]:
            continue
        name = index["name"].replace('"', '""')
        if [info[2] for info in conn.execute(f'PRAGMA index_info("{name}")')] == ["id"]:
            return True
    return False


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        if not DB_PATH.exists():
            raise TaskIndexError(
                f"{DB_PATH} does not exist and --no-discover cannot build it. "
                "Run without --no-discover, or run `reindex`."
            )
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not read_only:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # Trust the shape, not just the number. A half-finished upgrade can leave
        # the version stamped and the old table still standing, and the cost of
        # being wrong here is every command failing on a missing column.
        if version == SCHEMA_VERSION and not schema_is_current(conn):
            version = -1
        if version != SCHEMA_VERSION:
            # Any other version is thrown away and built again from `tasks/`,
            # including 0 -- which is both a database this tool has never
            # touched and one left by the ledger design, whose `tasks` table
            # was keyed by slug and sat beside an `id_allocation` table.
            conn.executescript("DROP TABLE IF EXISTS tasks;"
                               "DROP TABLE IF EXISTS id_allocation")
        conn.executescript(SCHEMA)
        if version != SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


# --- the one discovery operation ---------------------------------------------

def discover_and_sync(conn: sqlite3.Connection) -> list[str]:
    """Reconcile the table with `tasks/`, and report what a caller must not ignore.

    Called by `query`, by `add` inside the transaction that allocates, by
    `reindex`, by the write commands that resolve a task argument, and by
    `check`. There is exactly one of these on purpose.

    Returned issues are *blocking* ones: a repeated number, and a directory
    prefix that disagrees with its frontmatter id. Both make "the number
    identifies the task" false, so allocation and number-based lookup stop
    rather than guess. Softer conditions -- a row whose directory is gone, an
    unreadable task.md -- are reported by `check` and never silently repaired,
    because deleting a row can mask a partial restore.
    """
    known = {row["path"]: row for row in conn.execute(
        "SELECT path, id, task_mtime_ns, task_size FROM tasks")}

    # Read the tree first, decide second. A repeated number has to be known
    # before anything is written, or the UNIQUE index reports it as a database
    # error instead of as the diagnosable tree condition it actually is.
    fingerprints: dict[str, tuple[int, int]] = {}
    claimed: dict[str, int | None] = {}
    fresh: dict[str, dict] = {}
    for directory in iter_task_dirs():
        path = f"tasks/{directory.name}"
        try:
            stat = (directory / "task.md").stat()
            fingerprints[path] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            fingerprints[path] = (0, 0)
        row = known.get(path)
        # The fingerprint shortcut is only safe for a row that carries a number.
        # A stored NULL means the last pass could not use this directory's claim
        # -- contested or unreadable -- and skipping it here would make that
        # condition invisible on every later run, which is how a contested pair
        # would quietly become two nameless rows and drag MAX(id) down with them.
        if (row is not None and row["id"] is not None
                and (row["task_mtime_ns"], row["task_size"]) == fingerprints[path]):
            claimed[path] = row["id"]
        else:
            fresh[path] = read_record(directory)
            claimed[path] = fresh[path]["id"]

    by_id: dict[int, list[str]] = {}
    for path, number in claimed.items():
        if number is not None:
            by_id.setdefault(number, []).append(path)
    ambiguous = {number for number, paths in by_id.items() if len(paths) > 1}

    # A move leaves the old path behind carrying the same unique id. Retire that
    # row rather than letting one task hold two. Only for an unambiguous id:
    # with a repeated number there is nothing to reconcile it against.
    for path, number in claimed.items():
        if number is None or number in ambiguous:
            continue
        for stale in conn.execute(
            "SELECT path FROM tasks WHERE id = ? AND path <> ?", (number, path)
        ).fetchall():
            if stale["path"] not in fingerprints:
                conn.execute("DELETE FROM tasks WHERE path = ?", (stale["path"],))

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for path in sorted(claimed):
        number = claimed[path]
        # A contested number identifies nothing, so it is not stored as an
        # identity. The directory stays indexed and visible; `issues` names it.
        effective = None if number in ambiguous else number
        record = fresh.get(path)
        if record is None:
            if effective == known[path]["id"]:
                continue
            record = read_record(TASKS_DIR / path[len("tasks/"):])
        conn.execute(
            "INSERT INTO tasks (path, id, slug, title, date, status, status_detail, request_id,"
            " projects_json, trips_json, task_mtime_ns, task_size, indexed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET"
            " id=excluded.id, slug=excluded.slug, title=excluded.title, date=excluded.date,"
            " status=excluded.status, status_detail=excluded.status_detail, request_id=excluded.request_id,"
            " projects_json=excluded.projects_json, trips_json=excluded.trips_json,"
            " task_mtime_ns=excluded.task_mtime_ns, task_size=excluded.task_size,"
            " indexed_at=excluded.indexed_at",
            (path, effective, record["slug"], record["title"], record["date"],
             record["status"], record["status_detail"], record["request_id"],
             json.dumps(record["projects"], ensure_ascii=False),
             json.dumps(record["trips"], ensure_ascii=False),
             fingerprints[path][0], fingerprints[path][1], now),
        )

    issues: list[str] = []
    for number in sorted(ambiguous):
        issues.append(f"id {number} is claimed by {len(by_id[number])} directories: "
                      + ", ".join(sorted(by_id[number])))
    for path in sorted(claimed):
        if claimed[path] is None and path not in {p for n in ambiguous for p in by_id[n]}:
            issues.append(f"{path}: no task number can be read from its frontmatter, so what "
                          "number it holds is unknown; repair task.md before issuing another")
    # The directory prefix and the frontmatter id must agree exactly. A usable id
    # in a directory that does not carry it is the failure the `2026-05-26-...`
    # rename was meant to end: `tasks/archive/task.md` with `id: 90000` used to
    # have no issue at all, join MAX(id), and pull the next number to 90001.
    for path in sorted(claimed):
        name = path[len("tasks/"):]
        number = claimed[path]
        if number is None:
            continue
        match = PREFIX_RE.match(name)
        if DATE_NAME_RE.match(name):
            # A year that happens to equal the id agrees with the prefix check and
            # still lies: a date is not a task number whatever the frontmatter says.
            issues.append(f"{path}: the directory is named by a date, not by the task number "
                          f"{number} its frontmatter claims; rename it to {number:03d}-<slug>")
        elif not match:
            issues.append(f"{path}: the directory name carries no task number while its "
                          f"frontmatter claims {number}; rename it to {number:03d}-<slug>")
        elif int(match.group(1)) != number:
            issues.append(f"{path}: directory number {int(match.group(1))} "
                          f"disagrees with frontmatter id {number}")
    return issues


@contextlib.contextmanager
def exclusive(conn: sqlite3.Connection):
    """The one write lock every mutation is held under, start to finish.

    `BEGIN IMMEDIATE` takes SQLite's write lock at once rather than on the first
    write, so a second process is refused the lock at the top of its command and
    waits there under `busy_timeout` instead of interleaving with this one. That
    is what makes the lock cover a *filesystem* mutation it knows nothing about:
    the database is not protecting its own rows here, it is the mutex the task
    tree does not have.

    An uncontended command pays one lock acquisition, which is what it already
    paid for discovery. Only a genuine second writer waits.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def synced(conn: sqlite3.Connection) -> list[str]:
    """discover_and_sync in its own transaction, for callers that do not own one."""
    with exclusive(conn):
        issues = discover_and_sync(conn)
    return issues


def require_unambiguous(issues: list[str]) -> None:
    if issues:
        raise TaskIndexError(
            "the task tree is ambiguous and no number can be issued or resolved "
            "until it is repaired:\n  " + "\n  ".join(issues))


# --- resolving a task argument ------------------------------------------------

def resolve(conn: sqlite3.Connection, token: str) -> Path:
    """A task number, a directory name, or a repo-relative path.

    Numbers stay the preferred reference: renaming a directory must not threaten
    a cross-task reference.

    The caller owns the transaction. Resolution is the first half of a
    read-modify-write over `task.md`, and closing discovery's transaction here
    would publish the answer to a question the caller is still acting on: the
    directory this returns is about to be rewritten, and the write must not
    begin outside the lock that established which directory it is.
    """
    issues = discover_and_sync(conn)
    if token.isdigit():
        require_unambiguous(issues)
        found = conn.execute("SELECT path FROM tasks WHERE id = ?", (int(token),)).fetchall()
        if not found:
            raise TaskIndexError(f"no task with number {int(token)}")
        return contained(REPO_ROOT / found[0]["path"])
    name = token[len("tasks/"):] if token.startswith("tasks/") else token
    directory = contained(TASKS_DIR / name)
    if not directory.is_dir():
        raise TaskIndexError(f"no task directory {token}")
    return directory


def contained(path: Path) -> Path:
    """Strict containment. A task path that escapes `tasks/` is not a task path."""
    resolved = Path(os.path.normpath(path))
    if resolved.parent != TASKS_DIR or resolved.name in ("", ".", ".."):
        raise TaskIndexError(f"path escapes {TASKS_DIR}: {path}")
    return resolved


def validate_links(values: list[str]) -> list[str]:
    """Project and trip links are repo-relative paths inside the repository."""
    out = []
    for value in values:
        value = value.strip()
        if not value:
            continue
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise TaskIndexError(f"link must be a repo-relative path inside the repository: {value}")
        if not (REPO_ROOT / value).exists():
            raise TaskIndexError(f"link does not exist: {value}")
        out.append(value)
    return out


# --- writing frontmatter ------------------------------------------------------

def key_spans(block: str) -> dict[str, list[tuple[int, int]]]:
    """The inclusive line span of each top-level key's complete YAML node.

    A replacement has to consume the whole node, not the `key:` line: a block
    sequence, a block scalar, a wrapped plain scalar and a multi-line flow
    collection all continue past it. Replacing the first line alone is what left
    the 44 tasks carrying a block-list `projects:` unwritable -- the orphaned
    `- data/...` lines turned the candidate into a list whose first element was a
    mapping, and the parse gate refused the write.

    This reads the block through PyYAML's composer, which stops at nodes and
    constructs nothing, and uses the marks it already computes. It is still a
    *read*: the document is never re-serialized, so quoting, key order and
    comments elsewhere in the block are copied through byte for byte.

    PyYAML's scanner skips comments, so a block collection's end mark lands on
    the next key and swallows any comment lines between the two. They are pushed
    back out of the span, along with blank lines, so they stay with the file
    rather than with whichever key happens to precede them.

    Inside a literal or folded scalar that pushback would be wrong: an indented
    `#` line there is *data*. Backing it out left the tail of the old value in
    the file, so `status_detail: |` ending in `  # private old detail` kept that
    line after a `set-status` that exited 0 -- the node was not replaced whole,
    which is the one thing this function claims to do. A block scalar therefore
    keeps its `#` lines and gives up only trailing blanks, which clip chomping
    has already dropped from the value.
    """
    try:
        node = yaml.compose(block, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        raise TaskIndexError(f"unreadable YAML frontmatter: {str(error).splitlines()[0]}")
    if not isinstance(node, yaml.MappingNode):
        return {}
    lines = block.split("\n")
    spans: dict[str, list[tuple[int, int]]] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        start = key_node.start_mark.line
        last = value_node.end_mark.line
        # A block collection or scalar ends at column 0 of the line *after* it.
        if value_node.end_mark.column == 0 and last > start:
            last -= 1
        literal = isinstance(value_node, yaml.ScalarNode) and value_node.style in ("|", ">")
        while last > start:
            stripped = lines[last].strip()
            if stripped and not (stripped.startswith("#") and not literal):
                break
            last -= 1
        spans.setdefault(key_node.value, []).append((start, last))
    return spans


def set_frontmatter(task_dir: Path, updates: dict[str, str]) -> None:
    """Replace the complete node of each key being changed, then prove it still parses.

    A write PyYAML would reject is refused with nothing changed rather than
    applied at exit 0.
    """
    task_md = task_dir / "task.md"
    text = task_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise TaskIndexError(f"{task_md} has no YAML frontmatter to update")
    end = text.find("\n---", 3)
    if end < 0:
        raise TaskIndexError(f"{task_md} has unterminated YAML frontmatter")

    block = text[3:end]
    lines = block.split("\n")
    spans = key_spans(block)
    # Back to front, so an earlier span's indices survive a later replacement.
    # A key repeated in the block gets every occurrence replaced: YAML keeps the
    # last, and leaving the earlier ones behind would contradict the read-back.
    for (start, last), key in sorted(
        ((span, key) for key, found in spans.items() if key in updates for span in found),
        reverse=True,
    ):
        lines[start:last + 1] = [f"{key}: {updates[key]}"]
    for key, value in updates.items():
        if key not in spans:
            lines.append(f"{key}: {value}")

    candidate = "---" + "\n".join(lines) + text[end:]
    fields, error = read_frontmatter(candidate)
    if error:
        raise TaskIndexError(f"refusing to write {task_md}: the result would be {error}")
    for key, value in updates.items():
        if fields.get(key) != yaml.safe_load(value):
            raise TaskIndexError(f"refusing to write {task_md}: `{key}` did not read back")

    replace_text(task_md, candidate)


def replace_text(task_md: Path, text: str) -> None:
    """Put `text` in place of `task_md`, whole or not at all.

    Per operation, not per file. `task.md.tmp` was shared by every process, so
    the loser of a race replaced a staging file the winner had already renamed
    away and died on FileNotFoundError. The write commands are serialized now
    and this is defence in depth behind that, not the fix: it also keeps a
    crashed write from leaving a name the next one would silently reuse.

    `cmd_rename` restores its saved `task.md` through this same function, so a
    rollback is written exactly as carefully as the write it is undoing.
    """
    temporary = task_md.with_name(f".{task_md.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, task_md)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def refresh_row(conn: sqlite3.Connection, task_dir: Path) -> None:
    """One task's row, after its frontmatter changed. Same shape discovery writes."""
    record = read_record(task_dir)
    try:
        stat = (task_dir / "task.md").stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        fingerprint = (0, 0)
    conn.execute(
        "INSERT INTO tasks (path, id, slug, title, date, status, status_detail, request_id,"
        " projects_json, trips_json, task_mtime_ns, task_size, indexed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET"
        " id=excluded.id, slug=excluded.slug, title=excluded.title, date=excluded.date,"
        " status=excluded.status, status_detail=excluded.status_detail, request_id=excluded.request_id,"
        " projects_json=excluded.projects_json, trips_json=excluded.trips_json,"
        " task_mtime_ns=excluded.task_mtime_ns, task_size=excluded.task_size,"
        " indexed_at=excluded.indexed_at",
        (record["path"], record["id"], record["slug"], record["title"], record["date"],
         record["status"], record["status_detail"], record["request_id"],
         json.dumps(record["projects"], ensure_ascii=False),
         json.dumps(record["trips"], ensure_ascii=False),
         fingerprint[0], fingerprint[1],
         _dt.datetime.now(_dt.timezone.utc).isoformat()),
    )


SINCE_RE = re.compile(r"^(\d+)([dw])$")


def since_cutoff(value: str, today: _dt.date | None = None) -> str:
    """The inclusive lower bound of `--since`, as text the `date` column compares to.

    `10d` means `date >= today - 10 days`, cutoff day included. The column holds
    ISO dates as text, so a text comparison is a date comparison.

    This is the task's *creation* date, which is what the frontmatter carries. It
    is a proxy for "recently worked on", and a good one: a task is usually
    created when its work starts. It is not a modification time, and the index
    deliberately does not track one.
    """
    value = value.strip().lower()
    today = today or _dt.date.today()
    match = SINCE_RE.match(value)
    if match:
        days = int(match.group(1)) * (7 if match.group(2) == "w" else 1)
        return (today - _dt.timedelta(days=days)).isoformat()
    try:
        return _dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise TaskIndexError(
            f"--since takes a relative window (`10d`, `2w`) or an ISO date "
            f"(`2026-07-19`), not {value!r}")


def slugify(title: str) -> str:
    """The directory name a title normalizes to, or "" when nothing survives.

    Callers decide what an empty result means: `add` is naming a directory that
    does not exist yet and falls back to `task`; `rename` is moving one that
    already has a usable name and refuses.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return "-".join(slug.split("-")[:8])


# --- commands -----------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> int:
    """Discovery and allocation in one transaction, then a directory, then the row.

    SQLite and the filesystem share no transaction, so recovery is part of the
    contract rather than an afterthought: the hidden staging directory is
    invisible to discovery, the atomic rename publishes the task, and a crash
    between that rename and the commit leaves a directory the next discovery
    picks up. A number is never free merely because no row mentions it.
    """
    if args.status not in CANONICAL_STATUSES:
        raise TaskIndexError(f"status must be one of {', '.join(CANONICAL_STATUSES)}")
    if args.request_id is not None:
        args.request_id = args.request_id.strip()
        if not args.request_id or len(args.request_id) > 200:
            raise TaskIndexError("--request-id must be a non-empty string of at most 200 characters")
    projects = validate_links(args.project or [])
    trips = validate_links(args.trip or [])
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    staging = TASKS_DIR / f".add-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    published = None
    try:
        with exclusive(conn):
            require_unambiguous(discover_and_sync(conn))
            if args.request_id:
                existing = conn.execute(
                    "SELECT id, slug, title, path FROM tasks WHERE request_id = ?",
                    (args.request_id,),
                ).fetchone()
                if existing is not None:
                    number = existing["id"]
                    slug = existing["slug"]
                    title = existing["title"]
                    published = contained(REPO_ROOT / existing["path"])
                    reused = True
                else:
                    reused = False
            else:
                reused = False
            if not reused:
                number = (conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM tasks").fetchone()[0] or 0) + 1
            # A caller that must create the task before it understands the request
            # passes an empty title and settles the name later with `set-title`.
                title = args.title.strip() or f"Task {number}"
                slug = f"{number:03d}-{args.slug or slugify(args.title) or 'task'}"
                target = contained(TASKS_DIR / slug)
                if target.exists():
                    raise TaskIndexError(f"refusing to clobber an existing directory: tasks/{slug}")

                staging.mkdir(parents=True)
                frontmatter = "".join((
                    "---\n",
                    f"id: {number}\n",
                    f"slug: {json.dumps(slug, ensure_ascii=False)}\n",
                    f"title: {json.dumps(title, ensure_ascii=False)}\n",
                    f"date: {_dt.date.today().isoformat()}\n",
                    f"status: {json.dumps(args.status)}\n",
                    (
                        f"request_id: {json.dumps(args.request_id, ensure_ascii=False)}\n"
                        if args.request_id else ""
                    ),
                    f"projects: {json.dumps(projects, ensure_ascii=False)}\n",
                    f"trips: {json.dumps(trips, ensure_ascii=False)}\n",
                    "---\n",
                ))
            # A repo-relative path written straight into the body resolves *below*
            # the task directory, so every generated link was broken. The frontmatter
            # keeps the repo-relative form; the body link is relative to where it sits.
                def link(target: str) -> str:
                    return f"- [{Path(target).parent.name}]({os.path.relpath(target, f'tasks/{slug}')})"

                body = TASK_TEMPLATE.format(
                    title=title, summary=args.summary,
                    projects="\n".join(link(p) for p in projects) or "- none",
                    trips="\n".join(link(t) for t in trips) or "- none",
                )
                (staging / "task.md").write_text(frontmatter + body, encoding="utf-8")
                (staging / "plan.md").write_text(
                    PLAN_TEMPLATE.format(summary=args.summary), encoding="utf-8")

                os.rename(staging, target)
                published = target
                refresh_row(conn, target)
    except BaseException:
        if published is None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        conn.close()

    if args.json:
        # `path` is repo-relative here and in `query`, so the key means one thing
        # everywhere. A caller running from another directory joins it to the
        # workspace root it already had to know to invoke this script.
        print(json.dumps({"id": number, "slug": slug, "title": title,
                          "path": f"tasks/{slug}", "reused": reused}, ensure_ascii=False))
    else:
        print(f"tasks/{slug}")
    return 0


def _write_command(token: str, updates, announce, after_write=None) -> int:
    """One lock from resolution through frontmatter replacement and row refresh.

    `updates` is a mapping, or a callable given the resolved directory:
    `set-projects --add` has to read the existing list before it can append to
    it, and the directory is not known until the token is resolved. That read
    and the write that follows it are one read-modify-write and are inside the
    lock together, so a concurrent `--add` cannot drop the link this one just
    read past.

    Every command used to close discovery's transaction, replace `task.md` with
    no lock held at all, and then open a second transaction only to refresh the
    row. Two commands against one task both announced success and the later
    whole-file replacement discarded the earlier one's field -- in a repository
    explicitly designed for concurrent child agents, which is the whole reason
    metadata writes were put behind commands.

    SQLite and the filesystem still share no transaction, so the ordering is
    deliberate: `task.md` is the source of truth and is written first, the row
    second. A crash between them leaves a fingerprint the next discovery
    notices, which is the same recovery `add` relies on.
    """
    conn = connect()
    try:
        with exclusive(conn):
            task_dir = resolve(conn, token)
            set_frontmatter(task_dir, updates(task_dir) if callable(updates) else updates)
            refresh_row(conn, task_dir)
    finally:
        conn.close()
    if after_write is not None:
        after_write(task_dir)
    print(announce(task_dir))
    return 0


def _retry_completed_workspace_cleanup(task_dir: Path) -> None:
    """Retry the runner-owned cleanup when a finished task closes later."""
    try:
        try:
            from task_agent import task_workspace
        except ImportError:
            scripts = Path(__file__).resolve().parents[2] / "task-runner" / "scripts"
            sys.path.insert(0, str(scripts))
            import task_workspace  # type: ignore[no-redef]
        task_workspace.record_completed_workspace_cleanup(
            task_dir,
            require_finished_run=True,
        )
    except Exception as exc:
        trace_path = task_dir / "trace.md"
        if not trace_path.exists():
            trace_path.write_text("# Trace\n\n", encoding="utf-8")
        timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
        with trace_path.open("a", encoding="utf-8") as trace:
            trace.write(
                f"- {timestamp} Terminal workspace cleanup: retained "
                f"(cleanup_error: {exc}).\n"
            )


def cmd_set_status(args: argparse.Namespace) -> int:
    """The qualifier belongs to the status it was attached to.

    The policy is: `--detail` sets it, and a transition without `--detail` clears
    whatever the previous status left behind. Carrying it forward is how
    `blocked (waiting)` became `completed (waiting)` -- a state no command ever
    announced and nothing on disk explains. A task that carries no qualifier is
    not given an empty one, so an ordinary transition still rewrites one line.
    """
    if args.status not in CANONICAL_STATUSES:
        raise TaskIndexError(f"status must be one of {', '.join(CANONICAL_STATUSES)}")

    def updates(task_dir: Path) -> dict[str, str]:
        changes = {"status": json.dumps(args.status)}
        if args.detail:
            changes["status_detail"] = json.dumps(args.detail)
        elif read_record(task_dir)["status_detail"]:
            changes["status_detail"] = "null"
        return changes

    detail = f" ({args.detail})" if args.detail else ""
    return _write_command(
        args.slug,
        updates,
        lambda d: f"{d.name}: {args.status}{detail}",
        _retry_completed_workspace_cleanup
        if args.status in {"completed", "cancelled"}
        else None,
    )


def cmd_set_title(args: argparse.Namespace) -> int:
    # An empty title exits 0 and changes nothing a reader can see: lookup falls
    # straight back to the Markdown heading. Refuse it instead of announcing it.
    if not args.title.strip():
        raise TaskIndexError("title must not be empty")
    return _write_command(args.slug, {"title": json.dumps(args.title, ensure_ascii=False)},
                          lambda d: f"{d.name}: {args.title}")


def cmd_set_links(args: argparse.Namespace, key: str) -> int:
    given = validate_links(args.path)
    final: list[str] = []

    def updates(task_dir: Path) -> dict[str, str]:
        # `--add` appends without duplicating; without it the list is replaced,
        # and no path at all clears it.
        record = read_record(task_dir)
        if args.add and record["_error"]:
            raise TaskIndexError(f"cannot append to `{key}` in tasks/{task_dir.name}: "
                                 f"{record['_error']}")
        existing = record[key] if args.add else []
        final[:] = existing + [value for value in given if value not in existing]
        return {key: json.dumps(final, ensure_ascii=False)}

    return _write_command(args.slug, updates, lambda d: f"{d.name}: {key} = {final or '[]'}")


def cmd_rename(args: argparse.Namespace) -> int:
    """The number survives; the directory name, slug and path move together.

    Held under the same single lock as every other write. A rename moves three
    things that have to agree -- the frontmatter `slug`, the directory name and
    the indexed path -- so a concurrent command that resolved the old directory
    between them would write into a path that no longer exists.

    The frontmatter and the directory cannot move in one transaction, so one of
    the two always lands first and a system error on the second leaves the pair
    disagreeing. Writing the directory first was tried and produced the mirror
    defect: the tree renamed and the `slug` stale (task 519, finding A6).
    Swapping back would only trade one fragment for the other. What closes it is
    recovery: `task.md` is the only thing changed when `os.rename` raises, its
    previous bytes are still in hand, and putting them back ends the command
    exactly where it started. A restore that itself fails is not swallowed --
    both errors and the resulting state are reported, and `check` names the
    surviving divergence.
    """
    conn = connect()
    try:
        with exclusive(conn):
            task_dir = resolve(conn, args.slug)
            record = read_record(task_dir)
            if record["id"] is None:
                raise TaskIndexError(f"{task_dir.name} has no usable frontmatter id to keep")
            # Normalize, and refuse what normalizes to nothing rather than collapsing
            # the directory to `NNN-task`. `add` may still fall back, because it is
            # naming a directory that does not exist yet; a rename is moving one that
            # already has a name worth keeping.
            new_slug = slugify(args.new_slug)
            if not new_slug:
                raise TaskIndexError(
                    f"the new slug normalizes to nothing: {args.new_slug!r}; "
                    "pass an ASCII slug, or keep the directory name it already has")
            target = contained(TASKS_DIR / f"{record['id']:03d}-{new_slug}")
            if target.exists():
                raise TaskIndexError(
                    f"refusing to clobber an existing directory: tasks/{target.name}")

            updates = {"slug": json.dumps(target.name, ensure_ascii=False)}
            if args.title:
                updates["title"] = json.dumps(args.title, ensure_ascii=False)
            task_md = task_dir / "task.md"
            before = task_md.read_text(encoding="utf-8")
            set_frontmatter(task_dir, updates)
            try:
                os.rename(task_dir, target)
            except BaseException as error:
                try:
                    replace_text(task_md, before)
                except BaseException as restore_error:
                    raise TaskIndexError(
                        f"tasks/{task_dir.name} could not be renamed to tasks/{target.name}"
                        f" ({error}), and rolling its frontmatter back failed"
                        f" ({restore_error}).\n"
                        f"The directory is still tasks/{task_dir.name}, but its frontmatter"
                        f" `slug` now reads {target.name!r}. Nothing else changed: the number,"
                        " the contents and the index row are intact. Repair it by hand --"
                        f" either set `slug` back to {task_dir.name!r} or complete the move --"
                        " and `check` will report the divergence until you do."
                    ) from error
                raise
            conn.execute("DELETE FROM tasks WHERE path = ?", (f"tasks/{task_dir.name}",))
            refresh_row(conn, target)
    finally:
        conn.close()
    print(f"tasks/{target.name}")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    """Throw the table away and build it again, all or nothing."""
    conn = connect()
    try:
        with exclusive(conn):
            conn.execute("DELETE FROM tasks")
            issues = discover_and_sync(conn)
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()
    print(f"indexed {count} tasks")
    for issue in issues:
        print(f"  issue: {issue}", file=sys.stderr)
    return 1 if issues else 0


def cmd_query(args: argparse.Namespace) -> int:
    conn = connect(read_only=args.no_discover)
    try:
        if args.no_discover:
            print("warning: --no-discover skips directory discovery; "
                  "this listing may be stale", file=sys.stderr)
        else:
            issues = synced(conn)
            if args.number is not None:
                # Looking a task up *by number* is exactly the question an
                # ambiguous tree cannot answer. Fail closed instead of guessing.
                require_unambiguous(issues)

        where, params = [], []
        if args.number is not None:
            where.append("id = ?")
            params.append(args.number)
        statuses = None
        if args.status and "all" not in args.status:
            statuses = [s for token in args.status
                        for s in (ACTIVE_STATUSES if token == "active" else (token,))]
            where.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        for option, column in (("project", "projects_json"), ("trip", "trips_json")):
            value = getattr(args, option)
            if value:
                # Compare elements, not text. `LIKE '%example%'` answers a question
                # nobody asked and quietly widens the result. A short name is
                # still an exact question -- it names a durable record, so it is
                # resolved to the stored paths that record owns and then matched
                # element for element like any other.
                targets = [value]
                if "/" not in value:
                    targets = sorted({
                        stored for row in conn.execute(f"SELECT {column} AS links FROM tasks")
                        for stored in json.loads(row["links"])
                        if Path(stored).parent.name == value
                    }) or [value]
                where.append(f"EXISTS (SELECT 1 FROM json_each({column}) WHERE value IN (%s))"
                             % ",".join("?" * len(targets)))
                params.extend(targets)
        if args.search:
            where.append("(title LIKE ? OR slug LIKE ?)")
            params.extend([f"%{args.search}%", f"%{args.search}%"])
        # `is not None`, not truthiness: `--since ""` must be refused, not
        # quietly dropped into an unfiltered listing that looks filtered.
        cutoff = since_cutoff(args.since) if args.since is not None else None
        if cutoff:
            # A task with no date cannot be placed in time, so it cannot be shown
            # to be recent. It stays visible everywhere else.
            where.append("date IS NOT NULL AND date >= ?")
            params.append(cutoff)

        sql = "SELECT * FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()

    records = [{
        "path": row["path"], "id": row["id"], "slug": row["slug"], "title": row["title"],
        "date": row["date"], "status": row["status"], "status_detail": row["status_detail"],
        "projects": json.loads(row["projects_json"]), "trips": json.loads(row["trips_json"]),
    } for row in rows]
    if cutoff:
        # A recency window is answering "what did we do lately", so it orders by
        # when, not by number. Tasks 001-022 are recent work carrying low
        # numbers -- id order is not chronology and never was. Two passes,
        # because the tie-breaker runs the other way: sort is stable, so the
        # ascending path survives inside each equal (date, id) group.
        records.sort(key=lambda r: r["path"])
        records.sort(key=lambda r: (r["date"] or "", r["id"] if r["id"] is not None else -1),
                     reverse=True)
    else:
        records.sort(key=lambda r: (r["id"] is not None, r["id"] or 0, r["slug"]), reverse=True)
    if args.limit:
        records = records[: args.limit]

    if args.format == "json":
        print(json.dumps(records, ensure_ascii=False, indent=2))
    elif args.format == "paths":
        for record in records:
            print(record["path"])
    elif args.format == "compact":
        label = "+".join(args.status) if args.status else "all"
        if cutoff:
            label += f", since {cutoff}, newest first"
        print(f"# tasks: {len(records)} matching ({label}) of {total} total")
        for record in records:
            extra = ""
            if record["projects"]:
                extra = " @" + ",".join(Path(p).parent.name for p in record["projects"])
            if record["trips"]:
                extra += " ~" + ",".join(Path(t).parent.name for t in record["trips"])
            print(f"{_number(record)} {record['date'] or '-'} {_status(record)} "
                  f"{record['title']}{extra}")
    else:
        for record in records:
            print(f"{_number(record)}  {record['date'] or '-'}  {_status(record):<28}  "
                  f"{record['path']}")
            print(f"      {record['title']}")
    return 0


def _number(record: dict) -> str:
    return f"{record['id']:03d}" if record["id"] is not None else "---"


def _status(record: dict) -> str:
    return f"{record['status']} ({record['status_detail']})" if record["status_detail"] \
        else record["status"]


def cmd_check(args: argparse.Namespace) -> int:
    """Report what still has consequences, and repair nothing.

    Anything that makes a number ambiguous stops the tool elsewhere, so it is an
    error here. A row whose directory is gone is reported and kept: deleting it
    would hide a partial restore, and if it held the highest number, a rebuild
    would hand that number out again.
    """
    conn = connect()
    try:
        problems = list(synced(conn))
        indexed = {row["path"] for row in conn.execute("SELECT path FROM tasks")}
        directories = {f"tasks/{d.name}" for d in iter_task_dirs()}
        for path in sorted(indexed - directories):
            problems.append(f"{path}: indexed but the directory is missing; "
                            "not removed automatically, because that can mask a partial restore")
        notes = []
        for directory in iter_task_dirs():
            record = read_record(directory)
            path = record["path"]
            if record["_error"]:
                problems.append(f"{path}: {record['_error']}")
            if record["id"] is None:
                problems.append(f"{path}: no usable frontmatter id")
                # With a usable id, discovery already named the mismatch and
                # stopped allocation; repeating it here says nothing new.
                if not PREFIX_RE.match(directory.name):
                    problems.append(f"{path}: directory name carries no task number")
            if record["status"] == UNKNOWN_STATUS:
                problems.append(f"{path}: status is not one of "
                                f"{', '.join(CANONICAL_STATUSES)}")
            # The raw frontmatter field, deliberately not `record["slug"]`: that
            # one is derived from the directory name and therefore agrees with it
            # by construction, which is exactly why a half-applied `rename` used
            # to pass this check in silence. A missing key says nothing to
            # compare; a key that is present and disagrees is a rename that
            # stopped between the file and the directory.
            declared = record["_fields"].get("slug")
            if declared is not None and declared != directory.name:
                problems.append(f"{path}: frontmatter `slug` reads {declared!r} while the "
                                "directory is named otherwise; a rename applied to one and "
                                "not the other. Set `slug` to the directory name, or finish "
                                "the move with `rename`")
            task_md = directory / "task.md"
            for required in ("task.md", "plan.md"):
                if not (directory / required).is_file():
                    problems.append(f"{path}: {required} is missing")
            # A directory whose task.md is gone was just reported as such. Reading
            # it anyway turned that diagnostic into a FileNotFoundError traceback.
            if not task_md.is_file():
                continue
            body = task_md.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^## Status[ \t]*\r?\n(.*?)(?=\n## |\Z)", body, re.S | re.M)
            if match and record["status"] != UNKNOWN_STATUS:
                for word in CANONICAL_STATUSES:
                    if re.search(rf"\b{word}\b", match.group(1)) and word != record["status"]:
                        notes.append(f"{path}: body '## Status' reads '{word}' while the "
                                     f"frontmatter says '{record['status']}'; the frontmatter "
                                     "is authoritative and the body section is for prose")
                        break
        count = len(directories)
    finally:
        conn.close()

    print(f"checked {count} task directories")
    for note in notes:
        print(f"  note: {note}")
    for problem in problems:
        print(f"  issue: {problem}", file=sys.stderr)
    if problems:
        print(f"{len(problems)} issue(s)", file=sys.stderr)
        return 1
    print("no issues")
    return 0


# --- entry point --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="allocate a number and create the task directory")
    p_add.add_argument("title")
    p_add.add_argument("summary")
    p_add.add_argument("slug", nargs="?")
    p_add.add_argument("--project", action="append")
    p_add.add_argument("--trip", action="append")
    p_add.add_argument("--status", default="planned")
    p_add.add_argument(
        "--request-id",
        help="optional durable idempotency key; replay returns the originally allocated task",
    )
    p_add.add_argument("--json", action="store_true",
                       help="print {id, slug, title, path, reused} with a repo-relative path")
    p_add.set_defaults(func=cmd_add)

    p_status = sub.add_parser("set-status")
    p_status.add_argument("slug", help="task number, directory name, or path")
    p_status.add_argument("status")
    p_status.add_argument("--detail")
    p_status.set_defaults(func=cmd_set_status)

    p_title = sub.add_parser("set-title")
    p_title.add_argument("slug")
    p_title.add_argument("title")
    p_title.set_defaults(func=cmd_set_title)

    for name, key in (("set-projects", "projects"), ("set-trips", "trips")):
        p_links = sub.add_parser(name)
        p_links.add_argument("slug")
        p_links.add_argument("path", nargs="*", help="no paths clears the list")
        p_links.add_argument("--add", action="store_true",
                             help="append without duplicating instead of replacing the list")
        p_links.set_defaults(func=lambda a, key=key: cmd_set_links(a, key))

    p_rename = sub.add_parser("rename", help="rename the directory; the number is kept")
    p_rename.add_argument("slug")
    p_rename.add_argument("new_slug", help="the part after the number")
    p_rename.add_argument("--title")
    p_rename.set_defaults(func=cmd_rename)

    p_query = sub.add_parser("query")
    p_query.add_argument("--status", action="append",
                         help="a canonical status, 'active', or 'all'")
    p_query.add_argument("--number", type=int)
    p_query.add_argument("--project", help="a stored repo-relative path, or a durable "
                                           "record's directory name")
    p_query.add_argument("--trip", help="a stored repo-relative path, or a durable "
                                        "record's directory name")
    p_query.add_argument("--search")
    p_query.add_argument("--since", help="a recency window (`10d`, `2w`) or an ISO date; "
                                         "includes every status and orders by date, newest first")
    p_query.add_argument("--limit", type=int)
    p_query.add_argument("--format", choices=("table", "compact", "json", "paths"),
                         default="table")
    p_query.add_argument("--no-discover", action="store_true",
                         help="read the table as it stands; the listing may be stale")
    p_query.set_defaults(func=cmd_query)

    sub.add_parser("reindex", help="rebuild the table from tasks/").set_defaults(func=cmd_reindex)
    sub.add_parser("check", help="report task directories that need attention").set_defaults(
        func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TaskIndexError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
