# Architecture Overview

This document describes the generic repository structure. Skill-specific behavior belongs in each skill's `SKILL.md`.

## Purpose

Task Agent is a task-first agent workspace. A concrete agent implementation may vary, but the durable artifacts should stay stable across tools.

## Tasks

Every non-trivial task has a directory under `tasks/` named:

```text
NNN-task-slug
```

Each task directory must contain:

- `task.md`
- `plan.md`

Optional task artifacts:

- `task_contract.json` for hard constraints, forbidden substitutions, required live evidence, and completion gates
- `trace.md` for chronological progress notes
- `status.json` for machine-readable progress state
- `.runner/runner.json` and `.runner/runner.log` for child-agent launch diagnostics
- `findings.md` and `sources.md` for research tasks
- `multi-agent/` for explicit multi-agent pipeline artifacts

The canonical task list is [tasks/INDEX.md](../tasks/INDEX.md).

## Durable Projects

Use `data/projects/<project-slug>/` when multiple tasks contribute to one reusable non-task outcome.

Recommended project files:

- `project.md`
- `status.md`
- `context.md`
- `decisions.md`
- `sources.md`
- `artifacts/`

Tasks should link related projects from both `task.md` and `tasks/INDEX.md`.

## Skills

Skills live under `skills/` as self-contained directories.

Core skills in this template:

- [task-creator](../skills/task-creator/SKILL.md)
- [task-runner](../skills/task-runner/SKILL.md)
- [project-organizer](../skills/project-organizer/SKILL.md)
- [repo-health](../skills/repo-health/SKILL.md)
- [skill-maintainer](../skills/skill-maintainer/SKILL.md)

Project-level docs may describe where skills live and how they interact with task artifacts. They should not duplicate the step-by-step behavior documented by a skill.

## Workflow

1. A non-trivial user request becomes a task artifact under `tasks/`.
2. The task is added to `tasks/INDEX.md`.
3. The parent agent prepares the task directory as the execution handoff.
4. A child CLI agent may perform substantial work and write progress artifacts into the task directory.
5. If the user explicitly requests a team-of-agents execution style, the parent may launch the task-runner multi-agent workflow.
6. When present, `task_contract.json` is propagated by the orchestrator into role prompts and used by review/completion gates.
7. If source files change, the finished source change is committed and pushed unless the task explicitly keeps it local or publication is blocked.
8. If engine behavior changes, project documentation is updated in the same change.

Implementation work should match the semantics of the requested target, not only an approximate effect. When a task names a reference artifact, provider, model, protocol feature, or runtime branch, repository artifacts and verification should show that the named path was used directly or should explicitly record why that was not possible.

For repository self-development work, see [self-development.md](./self-development.md).

## Runtime State

Persistent authenticated state and local secrets should not be stored in task directories. Use `.state/` or another local-only runtime location and keep it ignored by git.

## Template State

This public template tracks only skeleton task/data files. Real `tasks/` and `data/` contents are local durable artifacts and should be backed up by the operator's own backup flow.

After restoring durable state from backup, run:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py
```
