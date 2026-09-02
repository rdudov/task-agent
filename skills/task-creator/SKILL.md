---
name: task-creator
description: "Use this skill when you need to create, rename, re-status, or look up a task artifact in this project. It is the one callable interface for creating a task and for every task-metadata write: it allocates the next task number atomically from one SQLite table that is fully rebuildable from tasks/, creates the task directory with task.md and plan.md carrying YAML frontmatter, and keeps that table in step through lazy discovery. Also use it to list tasks by criteria instead of loading a whole index into context."
---

# Task Creator

This project stores every task as a dedicated artifact directory under `tasks/`.

There are two stores, and only one of them is authoritative:

```
tasks/<slug>/task.md frontmatter   source of truth (durable, human-readable, greppable)
        ^ every write goes here first        | discover_and_sync (lazy, on every command)
.state/tasks-index.db  tasks       one table, fully rebuildable from tasks/
```

`scripts/tasks_index.py` owns both. It is the one callable creation interface:
do not hand-assemble a `task.md`, and do not hand-edit the frontmatter when a
command exists for the field. Reading the index directly is fine; writing to it
is not.

## The Database Is Rebuildable, All Of It

Delete `.state/tasks-index.db` and the next command builds it again from
`tasks/` — including the next task number, which is `MAX(id) + 1` over the
table after it has been synced. A corrupt database is repaired by removing it.
Nothing here is the record of anything.

This replaced a design that split the same file in half: a disposable lookup
table beside an `id_allocation` ledger that was the allocator of record, never
re-derived from disk, with no repair path. Losing `.state/` while `tasks/`
survived left task creation broken with no way back. Do not reintroduce a
durable allocator under another name.

## One Discovery, Shared By Everything

`discover_and_sync` is the only thing that reads the task tree, and `query`,
`add`, `reindex`, write-command argument resolution and `check` all call it. It
lists the immediate directories of `tasks/` — never their payloads — compares
each `task.md` against a stored `mtime_ns`+`size` fingerprint, and parses YAML
only for what is new or changed.

**Do not implement discovery a second time.** `add` runs it inside the
`BEGIN IMMEDIATE` that computes the number, so a directory the table has not yet
seen still owns its number. An `add` that allocated from an unsynced table would
reissue a number that already exists on disk.

A task's `id` comes from its frontmatter and never from the directory name. A
directory with no usable number is indexed with a null id: visible in listings,
absent from `MAX(id)`, reported by `check`. The prefix and the `id:` must agree
exactly, and a directory that has a usable `id` must carry it in its name: a
name with no numeric prefix (`tasks/archive/`) or a date-shaped one
(`2026-05-26-…`) blocks allocation exactly as a mismatched prefix does. Both
used to pass — `tasks/archive/task.md` claiming `id: 90000` moved the next
number to 90001, and a date whose year equalled the `id` satisfied the prefix
comparison while still being a date.

## Self-Healing, But Failing Closed

Discovery adopts a directory with no row, re-reads one whose fingerprint
changed, and follows a task that moved while keeping its unique id. A row whose
directory has vanished is **reported and never deleted** — automatic deletion
masks a partial restore, and if it held the highest number a rebuild would
reissue it.

It never guesses. A number claimed by two directories, a prefix that disagrees
with its frontmatter, a directory that does not carry the number it claims, or a
`task.md` whose number cannot be read stops `add` and number-based lookup until
the tree is repaired. There is no amnesty list: the 23
legacy duplicates were resolved in the data by task 522 and `id` is `UNIQUE`.

Self-healing exists for crash recovery and restore. It does not make hand-editing
frontmatter supported — the commands below are still the only way.

## A Plain Query Writes

Lazy discovery is how the index heals, so `query` needs write access to
`.state/`. `query --no-discover` is the explicit read-only mode: it opens the
database `mode=ro` and warns on stderr that its listing may be stale. It is
never a silent fallback.

## There Is No Delete

Completion is `completed`, refusal is `cancelled`, archiving keeps the
directory. There is no delete command and no tombstone, and neither may be
added: `MAX(id) + 1` over a rebuildable table is sound only while directories
are durable.

## Creating A Task

```bash
skills/task-creator/scripts/create_task.sh "Task title" "Short task description"
```

Optional third argument overrides the slug; optional flags add durable links and
an initial status, and `--json` returns the allocation:

```bash
skills/task-creator/scripts/create_task.sh \
  "Task title" "Short task description" "custom-slug" \
  --status in_progress --json \
  --project data/projects/trimaran-autopilot/project.md \
  --trip data/trips/2026-01-01-example-trip/trip.md
```

This creates `tasks/NNN-slug/` with `task.md` and `plan.md` and writes the
frontmatter. `create_task.sh` is a thin wrapper around `tasks_index.py add`;
either is fine. `--json` prints `{"id", "slug", "title", "path", "reused"}`, which is what
a programmatic caller should consume instead of parsing a path.

The shell wrappers prefer this checkout's `.venv/bin/python`, because that is
where the Quick Start installs PyYAML. `TASK_AGENT_PYTHON` can select another
executable explicitly; only when neither exists do they fall back to `python3`
from `PATH`.

Remote intake callers must also pass a stable `--request-id VALUE`. The value is
stored in `task.md` frontmatter and indexed uniquely. Repeating `add` with the
same request id returns the original task with `"reused": true`; this remains
true after deleting and rebuilding `.state/tasks-index.db`. Do not substitute an
in-memory dedupe key or a state-database-only receipt.

The number is allocated inside a single exclusive SQLite transaction that first
syncs the table with `tasks/`, so two child agents creating a task at the same
moment cannot collide and neither can outrun a directory already on disk. 16
simultaneous `add` calls yield 16 distinct numbers. `add` stages the files in a
hidden directory that discovery ignores, publishes them with one atomic rename,
then writes the row — so a crash between the two leaves a directory the next
discovery adopts.

The original `find | sed | sort | tail` allocation had no locking and produced
23 duplicate numbers. They no longer exist; task 522 moved the less-referenced
member of each pair to a free number.

### When You Do Not Know The Title Yet

A caller that has to create the task before it understands the request passes an
empty title, takes the number, and settles the name afterwards:

```bash
skills/task-creator/scripts/tasks_index.py add "" "Short task description" --json
# -> {"id": 508, "slug": "508-task", "title": "Task 508", "path": "tasks/508-task"}

# later, once the work is understood -- one call settles both
skills/task-creator/scripts/tasks_index.py rename 508 "better-slug" --title "The real title"
# or just the title
skills/task-creator/scripts/tasks_index.py set-title 508 "The real title"
```

## Other Operations

```bash
# change status; --detail sets a qualifier such as verification_gap, and a
# transition without --detail clears the one the previous status left behind
skills/task-creator/scripts/tasks_index.py set-status <task> completed [--detail X]

# settle a title
skills/task-creator/scripts/tasks_index.py set-title <task> "The real title"

# link (or unlink) durable records on an existing task
skills/task-creator/scripts/tasks_index.py set-projects <task> data/projects/foo/project.md
skills/task-creator/scripts/tasks_index.py set-trips <task> data/trips/bar/trip.md [--add]

# rename a task directory, keeping its number
skills/task-creator/scripts/rename_task.sh 010 "better-slug"

# throw the table away and build it again from tasks/*/task.md, all or nothing
skills/task-creator/scripts/tasks_index.py reindex

# report anything that makes a number ambiguous or a task unreachable; exit 1 on issues
skills/task-creator/scripts/tasks_index.py check
```

For a finished runner, changing status to `completed` or `cancelled` also asks
the existing task-runner cleanup owner to re-evaluate its one exact admitted Git
workspace. A cleanup refusal is recorded and does not undo the metadata write;
multi-repository grants remain retained for explicit per-workspace handling.

`<task>` is a **task number**, a bare directory name, or a path. Prefer the
number: it is the task's identity, so a caller holding one is not broken by a
later rename. A number shared by two directories — the recorded legacy
duplicates — is reported rather than guessed at, so name one of them.

A path with a directory component must actually exist under this repository's
`tasks/`. These commands will not fall back to the basename: a path copied from a
sibling checkout or from a `restore-point/` copy is an error, not an instruction
to act on the same-named task here.

Use `rename_task.sh` (or `tasks_index.py rename`) instead of a manual `mv`, so
the frontmatter `slug` line and the index follow the move. `rename` refuses a new
slug that normalizes to nothing — a Cyrillic-only argument would otherwise
collapse the directory to `NNN-task`. `add` still falls back to `task` in that
case, because it is naming a directory that does not exist yet; pass an explicit
slug argument for a non-ASCII title.

The frontmatter write and the directory move cannot be one transaction, so
`rename` carries a recovery contract instead of an ordering trick. It writes
`task.md` first; if `os.rename` then fails, it puts the previous bytes back and
re-raises the original error, and the task ends exactly where it started. Only a
restore that fails too can leave the file and the directory disagreeing, and it
says so on stderr — the original error, the failed restore, which name is where,
and that the number and contents are intact. `check` reports that divergence as
an issue until it is repaired, comparing the raw frontmatter `slug` against the
directory name. Note that the `slug` reported by `query` is derived from the
directory name and therefore cannot show the mismatch; only `check` and the file
itself can.

This rollback is `rename`'s alone. It is the only command that changes a
published file and then the filesystem: `add` writes into a hidden staging
directory discovery ignores and publishes it with one atomic rename, and the
other write commands touch nothing but SQLite after `task.md`, which rolls its
own transaction back.

`set-projects` and `set-trips` are the write path for the `projects` / `trips`
frontmatter arrays after creation; `add --project/--trip` only covers creation
time. They replace the list by default, `--add` appends without duplicating, and
passing no path clears it. They write the frontmatter array only — the body's
`## Projects` / `## Trips` sections are prose, exactly like the body `## Status`
section, and the tool does not maintain a second carrier for a value the
frontmatter already owns.

## Listing Tasks By Criteria

Query the index instead of reading task files in bulk:

```bash
# the default context pack: the last ten days, every status, newest first
skills/task-creator/scripts/tasks_index.py query --since 10d --format compact

# unfinished work only -- the secondary view, not the default context pack
skills/task-creator/scripts/tasks_index.py query --status active --format compact

# one task by its number
skills/task-creator/scripts/tasks_index.py query --number 497 --format paths

# everything attached to a durable record
skills/task-creator/scripts/tasks_index.py query --project example-project --format compact

# search by title or slug
skills/task-creator/scripts/tasks_index.py query --search example-research --limit 20
```

`--status` accepts a canonical status, `active` (planned + in_progress +
blocked), or `all`, and may be repeated. Filters combine. `--format` is `table`,
`compact`, `json`, or `paths`; `json` is the machine-readable form intended for
external dashboards and automation.

`--since` takes a relative window (`10d`, `2w`) or an ISO date and means
`date >= cutoff`, cutoff day included. It is the one listing ordered by
recency — `date` descending, then id descending, then path — because task
numbers are not chronology: tasks 001-022 are recent work carrying low numbers.
It includes every status on purpose, since recently *completed* work is usually
what "we did this recently" refers to, and it skips a task with no `date`,
which cannot be shown to be recent. Every other listing keeps its id order.

**The `date` it compares is the task's creation date, not when work on it
finished.** The frontmatter carries no modification time and the index
deliberately does not track one, so a task created three weeks ago and
completed yesterday does *not* appear in `--since 10d`. Creation is a good
proxy for "recently worked on" and a poor substitute for it: treat the window
as a reading order, and when it yields no useful match, `grep` all Markdown
task artifacts below `tasks/`, across the whole tree.

`--project` and `--trip` take either a stored repo-relative path
(`data/projects/example-project/project.md`) or the durable record's directory
name (`example-project`). A short name is resolved to the paths that record
owns and then matched element for element, so neither form is ever a substring
match: `--project data/projects/example` names nothing and returns nothing.

The index holds no task content. To recover knowledge, `grep` over `tasks/`.

## Task File Format

`task.md` starts with frontmatter, then the body:

```markdown
---
id: 123
slug: "123-task-slug"
title: "<Task title>"
date: 2026-07-27
status: "planned"
projects: []
trips: []
---
# <Task title>

## Summary
<Short task description>

## Inputs
- <The user's own substantive words, quoted verbatim: what they asked for and
  which path must do it. A later reviewer reads this instead of trusting the
  derived summary, so paraphrase does not replace the quote.>
- <Key user-provided dimensions, constraints, options, assumptions, or acceptance criteria>

## Status
- none

## Parent Task
none

## Related Tasks
- none

## Open Questions
- none

## Projects
- none

## Trips
- none
```

The canonical statuses are `planned`, `in_progress`, `blocked`, `completed`,
and `cancelled`. Use the optional `status_detail` field for a qualifier such as
`verification_gap`; put longer explanations in the body's `## Status` section.
The qualifier belongs to the status it was attached to: `set-status --detail`
sets it, and a transition given no `--detail` clears whatever the previous
status left behind. Carrying it forward is how `blocked (waiting)` became
`completed (waiting)` — a state no command ever announced.
The full schema is documented in [docs/task-execution.md](../../docs/task-execution.md).

There is exactly one carrier of the status *value*: the frontmatter `status`
field. The body's `## Status` section is a prose slot for the longer explanation
and must not repeat the value — `set-status` only stores that value in frontmatter, so a status
word left in the body goes stale on the first transition. `check` reports a body
section that consists of nothing but a canonical status word disagreeing with the
frontmatter as a note. Task directories created before 2026-07-28 still carry a
status word there; treat the frontmatter as authoritative.

The tool reads seven keys and ignores everything else, including nested content
under keys it does not know, so an added `owner:` block is neither fatal nor able
to overwrite a schema field. **It never re-serializes the block**: a command that
changes a field replaces that key's complete YAML node and copies every other
byte through unchanged.

The node, not the `key:` line. A block sequence, a block scalar, a wrapped plain
scalar and a multi-line flow collection all continue past it, so replacing the
line alone orphaned the continuation and the parse gate refused the write — the
44 tasks whose `projects:` is a block list could not be updated at all. The span
is taken from PyYAML's composer, which reads marks and constructs nothing;
comments and blank lines that trail a value stay where they are rather than
being swallowed by the key in front of them.

A key it cannot read costs at most one field, never the task: `title` falls back
to the `# ` heading and then to the directory name, `status` falls back to
`unknown`, and `check` names the problem.

Use `planned` by default unless the user clearly asked to mark another state.

Each `plan.md` should follow this structure:

```markdown
# Plan

## Goal
<One-sentence goal>

## Steps
1. Understand the current context.
2. Implement the required change.
3. Verify the result.
```

## Notes

- A task's identity is its **number**, and since task 522 that holds without
  exception. Do not renumber existing tasks; the directory name may change, and
  the number is what other callers hold.
- **Deleting `.state/tasks-index.db` is safe.** The next command rebuilds the
  whole table from the `task.md` files, next task number included. That is the
  supported repair for a corrupt index.
- Task numbers are zero-padded to three digits but are not limited to three. A
  four-digit directory such as `1000-example` is a conforming name, and the
  low numbers `001`-`022` are real tasks moved there by task 522, not
  placeholders. The prefix is compared to the frontmatter `id:` numerically, so
  padding is presentation.
- A plain `query` runs discovery and therefore **writes**; every caller needs
  write access to `.state/`. Both bot services and CLI sessions run as root, so
  this costs nothing today. `query --no-discover` is the named read-only mode
  and says its listing may be stale. The database is deliberately not in WAL
  mode: a WAL database is three files where the backup expects one.
- The installed `task-agent-tasks-index`, including its package file executed
  by absolute path, resolves its task tree through the runner's `repo_root()`,
  exactly like the other installed paths: `TASK_AGENT_ROOT` wins when set,
  otherwise the current directory is the workspace. Direct source-checkout
  execution preserves the same `TASK_AGENT_ROOT` override and otherwise keeps
  its checkout-relative root; `TASKS_INDEX_ROOT` is only the index-specific test
  override.
- The `projects:` / `trips:` arrays may be written in either block or flow style
  (`projects: [a/b.md]`); both read back as lists. A scalar there is malformed —
  `check` names it, and `set-projects --add` refuses rather than overwriting it.
- There is no amnesty file and no accepted-duplicate path. Task 522 resolved the
  23 legacy duplicate numbers and the one pre-numbering directory in the data, so
  `id` is `UNIQUE` and every duplicate is a real error. Do not add code to
  tolerate one.
- Preserve user-provided execution-critical details in `task.md` instead of
  compressing them into a vague summary.
- Prefer flat tasks plus explicit `Parent Task` and `Related Tasks` links over
  nested task directories.
- Prefer linking durable entities instead of duplicating their context into task
  files.
- `scripts/tasks_index.py` **reads** frontmatter with PyYAML (`yaml.safe_load`)
  and **writes** it by replacing only the lines of the one key it changes. Do
  not make a write go through `yaml.dump`: that would rewrite the quoting, key
  order and comments of every existing `task.md` at once.
- **A write never leaves the block unreadable.** The candidate text is parsed
  before it is written, and a write whose result PyYAML would reject is refused
  with a non-zero exit and nothing changed. That is why `set-status` cannot
  report a status nothing can read back.
- `PyYAML` is a declared dependency in `requirements.txt` and `requirements.lock`.
  The wrappers run under whichever `python3` is on PATH, so that interpreter
  needs it too.
- A frontmatter block PyYAML rejects is reported by `check` as `frontmatter is
  not valid YAML` rather than half-read. The task stays in lookup under its
  fallback title, and a write command can still repair the offending line — but
  only a write that *does* repair it: one that would leave the block unreadable
  is refused, and `set-projects --add` refuses outright, because the links it
  would append to cannot be read.
- Tests: `skills/task-creator/tests/test_rebuild_contract.py` (the rebuild and
  fail-closed contract), `test_preserved_properties.py` (the protections the
  rewrite had to keep), and `test_review_findings.py` (the findings of the
  repeat review, task 526).
