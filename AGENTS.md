# Task Agent Project Rules

This repository contains a generic assistant workspace built around autonomous agent workflows.

## Core Rule

Any substantial change to the agent engine must update documentation for both agents and humans in the same change.

A substantial change includes:

- task lifecycle or task artifact structure
- skill discovery or skill execution rules
- agent orchestration behavior
- restore, resume, or publication conventions

If a change modifies one of these areas, update the relevant project docs before finishing the task.

## Documentation Boundaries

- `AGENTS.md` defines project-wide operating rules.
- `docs/` explains repository architecture and workflows.
- Each skill documents its own behavior in its own `SKILL.md`.

Do not duplicate skill-specific instructions in project-level docs.

## Task Conventions

- Every substantial user task gets its own directory under `tasks/`.
- The only default exception is a clearly trivial request that can be answered immediately without research, file edits, durable artifacts, or multi-step execution.
- `tasks/INDEX.md` is the canonical ordered task index.
- Each task directory must contain `task.md` and `plan.md`.
- Multi-agent or review-sensitive tasks may also include `task_contract.json` for structured non-negotiable constraints, forbidden substitutions, required live evidence, and completion policy.
- `task.md` should preserve original inputs that matter for execution, such as constraints, assumptions, acceptance criteria, and explicitly requested options.
- Keep tasks flat in `tasks/`; express hierarchy through `Parent Task` and `Related Tasks` in `task.md`.
- Task-specific findings and sources belong in the task directory.

## Durable Data

- Reusable data that should survive across tasks belongs under `data/`.
- Multi-task project records belong under `data/projects/`.
- Active durable projects should maintain a rolling status snapshot, typically `status.md`, when that context matters across tasks.
- Local task and data artifacts are not a substitute for committing and pushing source changes.

## Skills

- Skills live under `skills/`.
- Use `skills/task-creator/` to create task artifacts.
- Use `skills/task-runner/` to delegate substantial work to child agents or run the explicit multi-agent workflow.
- Use `skills/task-artifacts/` during task execution to update `verification.md`, `findings.md`, and related files at checkpoints (not only in chat).
- Use `skills/project-organizer/` for multi-task durable project records.
- Use `skills/skill-maintainer/` when adding, restoring, or changing skills.
- Use `skills/repo-health/` after restores, before publishing a sanitized template, or when task/data/skill artifacts may be inconsistent.

## Execution Contract

When a parent agent delegates a task to a child agent, the task directory is the source of truth.

Before delegation, the parent agent should ensure `task.md` and `plan.md` contain enough context for independent execution. If the task has non-negotiable constraints, forbidden substitutions, or mandatory live verification gates, record them in `task_contract.json`.

Substantial implementation work should preserve at least one no-mock end-to-end verification path for the primary function being changed. Services, APIs, daemons, and workers should include a smoke check against the real runtime entrypoint in the target launch mode.

Do not assume backward compatibility, legacy fallback behavior, or a compatibility migration unless the user request or task contract explicitly requires it.

Agents must preserve the semantic target of the request instead of substituting a nearby implementation that merely produces a similar surface effect. When the user names a reference behavior, artifact, provider, model, protocol feature, or runtime branch, implementation and verification must exercise that named path directly, or the task must record the deviation as a blocker or explicit scope change.

When an agent-caused mistake materially affects a task, the correction is not complete until the agent performs a short mistake review and updates the relevant rules, docs, skills, scripts, or task contract so the same failure is less likely to repeat. If the prevention change is broad, risky, or requires a significant redesign, record the analysis and present concrete options to the user before implementing it.

If behavior diverges by threshold, mode, provider, credential, feature flag, model path, transport, or fallback branch, each production-relevant branch touched by the task needs explicit validation evidence or an explicitly recorded verification gap.

If a task contract marks a live/no-mock verification path as required, environment-blocked execution of that path is a blocker for approval or completion, not a non-critical note.

Tests around helpers, fixtures, mocks, fake models, or test-only harnesses do not count as production validation for a branch that can realistically run in production.

When a task modifies git-tracked source in a repository with a configured remote, completion should include committing and pushing the finished change unless the user explicitly asks not to, the work is intentionally left uncommitted for review, or credentials/network/policy block the push. If push is deferred or blocked, the final response and task artifacts must say so explicitly.

If a local, unpushed commit is discovered to be wrong and there are no clear contraindications such as dependent work, shared review state, or user-owned changes on top, prefer rewriting local history with `git reset` or another explicit history repair over adding a revert commit that preserves noise. Before resetting, inspect the affected repository status and commit graph; after resetting, record the old and new HEADs in the task trace. Use a revert commit when the bad commit was already pushed/shared or when preserving an auditable public history is explicitly required.

After backup restore or before publishing a sanitized copy, run the repository health check and record any gaps in the active task artifacts before relying on local task, data, or skill state.

## Remote Safety

For remotely triggered work, prefer the least destructive interpretation that still satisfies the request.

- Normal engineering edits, refactors, and deletions of specifically requested files are allowed.
- Cross-project work is allowed when it is explicitly part of the request and limited to the relevant project.
- Clearly destructive broad actions are not allowed by default.
