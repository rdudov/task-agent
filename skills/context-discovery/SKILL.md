---
name: context-discovery
description: >-
  Discover durable prior context before broad codebase search, live checks, or
  describing an existing decision. Use for task, durable record, sibling
  repository, and architecture-decision lookup.
---

# Context Discovery

## Scope

Use before broad search, live checks, or describing, justifying, or summarizing
an already-made decision. A rule carries an outcome; the durable decision record
carries its reasons.

## Lookup Order

1. Read the current task and tasks linked through `Parent Task` and
   `Related Tasks`.
2. Run `skills/task-creator/scripts/tasks_index.py query --since 10d --format compact`,
   or `--search <term>` when a keyword is known. The recent window includes every
   status and is the primary catalog; `--status active` is only the secondary
   view for unfinished work.
3. For an indirect reference, open only useful recent candidates. Treat a
   candidate excluded by the task's time or scope as no useful match; on no
   useful match there, search all Markdown task artifacts below `tasks/`, across
   the whole tree. The window is a reading order, not a search boundary. `date`
   is the creation date, not when work finished: a task created three weeks ago
   and completed yesterday is outside `--since 10d`.
4. Read every relevant primary artifact that exists: `task.md`, `findings.md`,
   `verification.md`, and `sources.md`. When a candidate describes or reproduces
   an observation made by another task, follow that provenance to the original
   task and read its primary artifacts before concluding. Record the concrete
   prior outcome that changes the current work, including an accepted repair or
   result identifier when the primary record provides one.
5. Read linked records under `data/projects/` and `data/trips/`, and the local
   repository/path indexes under `data/`, whose format
   `data/local-projects.example.md` describes. For repository subsystems, read
   their decision records and use `query --project <record-name>` for reverse
   lookup.
6. Read the target repository's agent instructions — root `AGENTS.md` and hidden
   ones such as `.codex/` or `.claude/` — plus its tool/runtime configuration,
   containers, and CI before review or implementation. Judge syntax, build, and
   convention questions against the runtime that repository declares rather than
   remembered defaults; when a construct looks wrong but the declared runtime
   makes it valid, record the runtime-context gap instead of reporting breakage.
7. Search sibling repositories only after durable local context is exhausted.

Discovery is complete only after the relevant primary files listed in step 4
were opened, including the original observation behind a derived candidate, or
the required whole-tree fallback was exhausted, and the relevant prior outcome
and its identifier were recorded when found. A zero query, failed query, or
excluded candidate is not a completion result.

## Unavailable Catalog

Substitute rather than skip. If ordinary query cannot write lazy index updates,
use `query --no-discover` only when a database exists and say that it may be
stale. If the index is unavailable, list/search `tasks/` directly. A read-only
child with no shell reads paths already named in `task.md` and reports the wider
catalog as unreachable rather than empty. A failed lookup never proves that no
prior work exists.

## Promotion

Promote reusable outcomes: project decisions to `data/projects/`, bounded travel
to `data/trips/`, repository and path facts to a local index under `data/`,
repeatable procedure to a skill, universal behavior to a project rule such as
`.cursor/rules/*.mdc`, and one-off conclusions to the active task's
`findings.md`.
