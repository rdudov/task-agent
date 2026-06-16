---
name: project-organizer
description: Use this skill when work spans multiple tasks and needs a durable shared project record for context, decisions, sources, or reusable artifacts.
---

# Project Organizer

This skill maintains reusable project records outside task artifacts.

## Scope

Use this skill when a task requires:

- creating a durable project folder for work that spans multiple tasks
- storing shared context, decisions, or sources outside a single task directory
- accumulating reusable project artifacts that should outlive one task

Task-specific execution notes still belong in the active task directory. Reusable project data belongs under `data/projects/`.

When a task materially concerns a project, update the task's `Projects` section and the corresponding row in `tasks/INDEX.md` to link that durable project record.

## Storage Model

Projects live under:

```text
data/projects/<project-slug>/
```

Each project directory must contain:

- `project.md`

Recommended project artifacts:

- `status.md`
- `context.md`
- `decisions.md`
- `sources.md`
- `artifacts/`

## Create A Project

Use the bundled script:

```bash
.venv/bin/python skills/project-organizer/scripts/create_project.py \
  --title "Trimaran autopilot" \
  --slug "trimaran-autopilot"
```

Add optional files only when they have real durable value. Do not dump ephemeral task notes into the project record.

For active projects, prefer maintaining `status.md` as a rolling summary of:

- completed work
- in-progress or remaining work
- newly added durable outcomes or capabilities

## Practical Guidance

- Use projects for initiatives that need continuity across tasks.
- Use a durable project for multi-task repository capability work, such as skill-system improvements, task-runner evolution, restore hardening, or documentation consolidation.
- Do not use a project when a single task is sufficient and no durable shared context is needed.
- Keep `project.md` focused on stable scope, status, related tasks, and durable references.
- Keep `status.md` focused on the current snapshot so a human can quickly see what changed recently and what is still left.
- Move shared deliverables into `artifacts/` only when they should be reused across tasks; otherwise keep them in the originating task directory.
- When a task materially changes project scope or completion state, update `status.md` in the same change.
