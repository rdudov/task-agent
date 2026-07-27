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
- `progress.json` for substantive live progress during a long run
- `.runner/runner.json` and `.runner/runner.log` for child-agent launch diagnostics
- `findings.md` and `sources.md` for research tasks
- `deliverables/` and `deliverables/manifest.json` for explicitly requested output files
- `multi-agent/` for explicit multi-agent pipeline artifacts

The canonical task list is `tasks/INDEX.md`. It is local generated state and is not tracked by the template. Use [tasks/INDEX.example.md](../tasks/INDEX.example.md) as the format template.

`tasks/USER_PREFERENCES.md` sits beside the index and holds durable defaults for choices the user did not spell out. It is written only from explicit reusable instructions and is always overridden by the current request. See [tasks/USER_PREFERENCES.example.md](../tasks/USER_PREFERENCES.example.md).

### Internal Records Versus Requested Deliverables

These are two different classes of output, and conflating them is how a task reports success while the user never receives what they asked for.

`findings.md`, `verification.md`, `sources.md`, `trace.md`, `status.json`, and `progress.json` are internal diagnostics. They explain how the work went.

`deliverables/` holds what the user actually asked for. Registration in `deliverables/manifest.json`, not a filename extension, decides what counts. Only contained regular non-symlink files with content are eligible, and an internal record belongs there only when the user asked for that record itself. `skills/repo-health/scripts/check_deliverables.py` validates the part of that contract a local template can check; whether the *right* files were produced is a judgment only the agent that read the request can make.

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

## Local Lookup Indexes

Use local files under `data/` for durable lookup context that should survive across tasks, such as repository paths, important artifacts, recurring commands, and where to find prior work. Start from [data/local-projects.example.md](../data/local-projects.example.md) when you need a compact repository/path index.

## Skills

Skills live under `skills/` as self-contained directories.

Core skills in this template:

- [task-creator](../skills/task-creator/SKILL.md)
- [task-runner](../skills/task-runner/SKILL.md)
- [project-organizer](../skills/project-organizer/SKILL.md)
- [repo-health](../skills/repo-health/SKILL.md)
- [skill-maintainer](../skills/skill-maintainer/SKILL.md)

Project-level docs may describe where skills live and how they interact with task artifacts. They should not duplicate the step-by-step behavior documented by a skill.

## Agent Entry Points

Three CLI agents read three different files, and none of them reads the others'. The rule is that a rule has exactly one canonical home and every entry point reaches it by reference:

| Canonical source | Read directly by | Reaches other agents through |
| --- | --- | --- |
| `AGENTS.md` | Codex | `CLAUDE.md` → `@AGENTS.md` |
| `.cursor/rules/*.mdc` | Cursor | `CLAUDE.md` → `@.claude/imports/*.md` symlinks |
| `skills/*/SKILL.md` | this repository's convention | `.claude/skills` → `../skills` symlink |

`CLAUDE.md` contains no rules of its own, and `.claude/` contains nothing but symlinks. Edit the canonical file; never edit through `.claude/`, and never move a canonical file into it, or Codex and Cursor lose it. Adding a Cursor rule means adding both the symlink and the `CLAUDE.md` import line.

Both ways this wiring breaks are silent — Claude Code ignores a non-`.md` import and skips a dangling symlink without warning — so `check_repo_health.py` verifies it, and [claude-code-setup.md](./claude-code-setup.md) documents live probes to confirm what a fresh session actually loads.

## Child Runner Selection

The child runner follows the parent CLI agent: a Codex session delegates to a Codex child, a Claude session to a Claude child. That decision lives in `task_runner.py` rather than in prompt text, so a caller that never reads a skill gets the same behavior. Explicit `--runner` always wins, and every run records which rule decided.

Access level is expressed once through `--sandbox-mode` and mapped per runner, because Codex and Claude express confinement differently. Restricted Claude modes need a Linux host with `bubblewrap` and `socat` and fail closed without them rather than downgrading — a run that claims a boundary it never had is worse than a run that refuses to start.

The one absolute path the runner needs is the workspace root that full access reaches. It defaults to the parent of this checkout and is overridden with `TASK_AGENT_WORKSPACE_ROOT`.

## Workflow

1. A non-trivial user request becomes a task artifact under `tasks/`.
2. The task is added to `tasks/INDEX.md`.
3. The parent agent prepares the task directory as the execution handoff.
4. A child CLI agent may perform substantial work and write progress artifacts into the task directory.
5. If the user explicitly requests a team-of-agents execution style, the parent may launch the task-runner multi-agent workflow.
6. When present, `task_contract.json` is propagated by the orchestrator into role prompts and used by review/completion gates.
7. If source files change, the finished source change is committed and pushed unless the task explicitly keeps it local or publication is blocked; remote-backed repositories should be synced before branch/push and checked with the pre-push leak check before publication.
8. If engine behavior changes, project documentation is updated in the same change.

Implementation work should match the semantics of the requested target, not only an approximate effect. When a task names a reference artifact, provider, model, protocol feature, or runtime branch, repository artifacts and verification should show that the named path was used directly or should explicitly record why that was not possible.

A child that exits non-zero before writing a terminal state does not leave `running` behind: the runner records terminal `failed` with the child exit code, so a dead run never reads as work in progress.

For repository self-development work, see [self-development.md](./self-development.md).

## Runtime State

Persistent authenticated state and local secrets should not be stored in task directories. Use `.state/` or another local-only runtime location and keep it ignored by git.

## Template State

This public template tracks only skeleton task/data files. Real `tasks/` and `data/` contents are local durable artifacts and should be backed up by the operator's own backup flow.

After restoring durable state from backup, run:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py
```
