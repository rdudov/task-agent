---
name: repo-health
description: Use this skill after restores, before publishing a sanitized template, before pushing source changes, or when a task's registered deliverables need validating. It runs generic structural checks for tasks, docs, skills, dependencies, obvious secret leaks, and the deliverables contract.
---

# Repo Health

This skill checks whether the repository is structurally usable after restore or before handoff.

## Scope

Use it when:

- a backup restore may have lost local artifacts
- a public/template copy is being prepared
- task, data, or skill structure has changed
- an agent-engine change needs a quick repository health gate

## Checks

The bundled script validates:

- required root files: `AGENTS.md`, `README.md`, `.gitignore`, `requirements.txt`, and `requirements.lock`
- required docs: `docs/architecture.md` and `docs/task-execution.md`
- task index presence and task links when `tasks/INDEX.md` exists
- every task directory has `task.md` and `plan.md`
- every `skills/*/SKILL.md` has `name` and `description` frontmatter
- executable Python scripts parse in memory, so the check works on read-only
  checkouts and does not create `__pycache__` entries
- obvious secret-like content is absent from the files Git can publish: tracked
  files plus untracked, non-ignored files. The check reuses the pre-push guard's
  pattern definitions and fails closed if Git cannot establish that scope.

Ignored `tasks/` and `data/` contain durable local evidence, not publishable
source. Repository health therefore does not scan those histories for secrets;
`check_pre_push.py` still refuses real task/data artifacts in outgoing commits.

## Command

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py
```

For a sanitized template that intentionally omits local task history:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py --allow-empty-tasks
```

## Pre-Push Leak Check

Run before pushing source changes to a configured remote:

```bash
.venv/bin/python skills/repo-health/scripts/check_pre_push.py --remote origin
```

The check scans outgoing files for local task/data/state artifacts, environment files, private keys, and common token formats. It is a guardrail, not a complete security scanner; still review the outgoing diff before pushing.

Deployment-specific private project/task/trip names belong in ignored local
`.state/private-history-markers` (one literal per line), or in the file named by
`TASK_AGENT_PRIVATE_HISTORY_MARKERS`. Repository health and pre-push consume the
same local list without publishing it. Pre-push also refuses refs from another
remote and unknown ref namespaces, while allowing ordinary local heads, tags,
notes, and stash.

## Deliverables Check

Run before marking a task complete when the user requested output files:

```bash
.venv/bin/python skills/repo-health/scripts/check_deliverables.py tasks/001-example
```

It validates the part of the delivery contract a local template can enforce: the manifest parses, every registered entry is a bare basename resolving to a contained regular non-symlink file with content, nothing is registered twice, and the count and byte totals stay within `--max-files` and `--max-bytes`. Files sitting in `deliverables/` without registration are reported as a warning, because unregistered means undelivered.

It cannot judge *which* files should have been requested. That comparison against the request and its continuations stays with the agent. Enforcement that depends on a real transport — content identity across restarts, delivery deduplication, per-message limits — belongs to the runtime that owns that transport.

## Notes

- This is a structural check, not a full security scanner.
- If it reports missing task/data artifacts after restore, create a task to repair or document the restore gap before doing substantive work that depends on those artifacts.
- If the repository's task lifecycle, skill execution, orchestration, restore, or resume behavior changes, update project docs in the same source change.
