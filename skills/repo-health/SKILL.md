---
name: repo-health
description: Use this skill after restores, before publishing a sanitized template, or when repository task/data/skill artifacts may be inconsistent. It runs generic structural checks for tasks, docs, skills, dependencies, and obvious secret leaks.
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
- executable Python scripts parse with `py_compile`
- obvious secret-like keys are not present under `tasks/` or `data/`

## Command

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py
```

For a sanitized template that intentionally omits local task history:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py --allow-empty-tasks
```

## Notes

- This is a structural check, not a full security scanner.
- If it reports missing task/data artifacts after restore, create a task to repair or document the restore gap before doing substantive work that depends on those artifacts.
- If the repository's task lifecycle, skill execution, orchestration, restore, or resume behavior changes, update project docs in the same source change.
