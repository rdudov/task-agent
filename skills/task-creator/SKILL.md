---
name: task-creator
description: Use this skill when you need to create a new task artifact in this project. It creates the next numbered directory under tasks, writes task.md and plan.md, and updates tasks/INDEX.md.
---

# Task Creator

This project stores each non-trivial task as a dedicated directory under `tasks/`.

## Preferred Workflow

```bash
skills/task-creator/scripts/create_task.sh "Task title" "Short task description"
```

Optional custom slug:

```bash
skills/task-creator/scripts/create_task.sh "Task title" "Short task description" "custom-slug"
```

Optional durable project links:

```bash
skills/task-creator/scripts/create_task.sh \
  "Task title" \
  "Short task description" \
  --project data/projects/example/project.md
```

To safely rename a task directory and update the index:

```bash
skills/task-creator/scripts/rename_task.sh tasks/001-placeholder "better-slug"
```

## Task File Format

Each `task.md` should preserve execution-critical user inputs:

```markdown
# <Task title>

## Summary
<Short task description>

## Inputs
- <Key constraints, assumptions, acceptance criteria, or requested options>

## Open Questions
- none

## Status
planned

## Parent Task
none

## Related Tasks
- none

## Projects
- none
```

Each `plan.md` should include the goal and a short step list.

## Notes

- Keep the index chronological.
- Do not renumber existing tasks.
- If the initial task file is too sparse, update it before substantive execution begins.
- Prefer flat tasks plus explicit `Parent Task` and `Related Tasks` sections over nested task directories.
