---
name: task-executor
description: >-
  Execute one prepared task as a standard child agent: what to read before
  starting, how to keep the task directory true while working, and what must
  hold before reporting completion. Not for a dev-pipeline owner, which has its
  own lifecycle.
---

# Standard Task Executor

This skill is the ordered procedure, not a second rule set. Each step names the
file that owns the rule behind it. Where a step routes, follow the owner; where
it states a rule outright, no other file in this repository states it.

## Scope

Use this workflow inside a `standard` child launched by `skills/task-runner/`.
If the launch says `dev-pipeline`, stop: that profile owns its own lifecycle and
this skill would be a competing controller.

## Start

1. Read the exact `task.md`, `plan.md`, and `task_contract.json` paths from the
   launch envelope. Read the task root's `USER_PREFERENCES.md` when it exists.
2. Treat the original request and later continuations as one ordered semantic
   contract. Current explicit intent beats reusable preferences and history; a
   stale artifact is a synchronization defect to repair or report, not authority
   to ignore the user. `task.md` is a translation of that intent, not its
   source: when the derived statement is satisfiable by a path the user's own
   words do not name, say so at the start rather than after the work.
3. If execution-critical user inputs are missing from `task.md`, preserve them
   there before continuing. `skills/task-creator/scripts/tasks_index.py` is the
   only interface that writes task metadata.
4. Set `status.json` to active work and append a concise `trace.md` entry.

Under a read-only access profile, the launch envelope names the one directory
you may write — your own task directory. Keep every artifact and metadata write
inside it, use the shell for reading, searching, testing and those writes, and
never write to the repository or target repository under review. Report the
exact unavailable step only when something genuinely outside that directory is
required. `skills/task-runner/SKILL.md` owns what each access profile grants.

When `task_contract.json` declares an enforced review verdict, write the
technical review to `findings.md` yourself and end that file with exactly one
standalone `Verdict:` line, as `AGENTS.md` requires. A verdict present only in
the runner log or the final chat response cannot close the task.

## Work

- Follow `skills/task-artifacts/SKILL.md` for findings, sources, verification,
  deliverables, progress, and checkpoint triggers.
- Invoke `context-discovery` before broad search, live checks, or restating an
  existing decision. Do not copy its lookup procedure here.
- Keep task-specific artifacts in the task directory, never in `.runner/` or a
  target product repository. Product repositories contain only requested source,
  tests, public docs, and normal metadata.
- Keep `trace.md` chronological, `status.json` current, and `progress.json`
  substantive for a long run, in the form `docs/task-execution.md` documents.
- Learn a reusable user preference only from explicit reusable instruction or
  feedback, under the `USER_PREFERENCES.md` rule in `AGENTS.md`. Never promote a
  one-off output format. Current intent always wins.
- For source changes, preserve unrelated work, and use `skill-maintainer` for
  skill changes and `repo-health` for the final structural check. The
  documentation and publication obligations are `AGENTS.md`'s.
- If the work changes a service, worker, command, API, or another live user
  surface, add a required live-evidence item to `task_contract.json`, deploy the
  change, and verify it through that same surface. Unit tests and calls made
  directly against a handler boundary are regression evidence, not substitutes.
  A bounded restart of the affected service is part of the task and is not
  reserved for a host owner. If the check cannot pass, record `FAIL`, `GAP`, or
  `BLOCKED`, leave the task blocked, and name what was not checked, why, and
  what would unblock it.
- Correcting an agent-caused systemic mistake follows the systemic-review rule
  in `AGENTS.md`: identify the semantic target, production path, owning layer,
  divergence, correction scope, verification, and prevention before changing
  code, and repair the owner rather than a neighboring layer.

## Prepare Completion

1. Re-read the original request and every continuation; compare actual outputs
   and registered deliverables with the latest complete intent. Name, for each
   key decision visible in the result, which path and which participant actually
   made it. A result the user's named path did not decide is a substitution: it
   is a blocker to report, not a pass, however convincing its numbers are, and
   however honestly the named component was also executed on the way. Report it
   even when the derived `task.md` would accept it.
2. Finish every plan step and record every required live-evidence result with
   the `skills/task-artifacts/` helper. A failed or unavailable mandatory gate
   is `FAIL`, `GAP`, or `BLOCKED`, never a prose caveat on an `OK` result.
3. Refresh linked durable records under `data/` when state materially changed.
4. Set task frontmatter through `tasks_index.py set-status` to `completed` only
   when the work and gates are complete; otherwise set it to `blocked` and
   record the concrete reason. Set `status.json` consistently and append the
   final trace entry.

The runner independently refuses exit code 0 unless frontmatter is `completed`,
the plan has no unfinished marker, required evidence has a latest passing
result, and required review gates are established. That gate is authoritative;
this procedure explains how to satisfy it.
