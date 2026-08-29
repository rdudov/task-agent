# Self-Development Workflow

This document describes how to change the repository's own agent framework without losing task history, skill behavior, or verification quality.

## What Counts

A change is self-development work when it affects:

- task lifecycle or task artifact structure
- skill discovery, skill invocation, or skill validation
- parent-child runner behavior
- child-run supervision or workflow orchestration
- backup restore, health checks, or resume behavior
- documentation rules that affect future agents

## Required Practice

Create a normal task directory before substantive work begins. Capture the original request, hard constraints, expected compatibility or migration behavior, and verification requirements in `task.md`. Use `task_contract.json` when the change has non-negotiable behavior or mandatory live evidence.

Update project documentation in the same source change when engine behavior changes. Use:

- `AGENTS.md` for project-wide agent rules
- `docs/` for architecture and human-facing workflows
- a skill's own `SKILL.md` for skill-specific behavior

Do not duplicate detailed skill instructions in project-level docs.

When self-development work produces reusable lookup knowledge such as paths, commands, limits, or integration entrypoints, update the relevant local index, skill, or rule in the same change. One-off conclusions belong in the active task's findings.

## Verification

Before completion, run the narrow tests for changed scripts and the repository health check:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py
```

For task-runner changes:

```bash
PYTHONPATH=skills/task-runner/scripts .venv/bin/python -m pytest skills/task-runner/tests
```

If a runtime path changed, include a smoke check against the real entrypoint in its target launch mode. Fixture-only tests are not enough for production-reachable branches.

## Restore Checks

After restoring from backup, run the repo-health skill before relying on local artifacts. Confirm that:

- `tasks_index.py query` answers from the restored tasks and task links resolve
- task directories contain `task.md` and `plan.md`
- durable data expected by active tasks or projects exists under `data/`
- skills have valid manifests and scripts parse
- no obvious secrets were restored into `tasks/` or `data/`

Record restore gaps in a task artifact before continuing dependent work.

## Publication

When self-development changes modify git-tracked source in a repository with a remote, commit and push after verification unless the task explicitly requires a local-only state or publication is blocked. Before branching or pushing, fetch and sync with the target remote. Before push, run `skills/repo-health/scripts/check_pre_push.py` and review the outgoing diff. If publication is blocked, record the reason and current repository state in the task artifacts: the completion gate refuses a dirty or unpushed repository until the task's own `publication.json` names it, why it stays, and who will send it. See "Source Publication" in [task-execution.md](task-execution.md).
