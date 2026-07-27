# Task Agent

Task Agent is a small, forkable workspace for task-first autonomous-agent workflows.

It is intentionally generic: no private task history, no local data, and no bundled personal integrations. Project-level operating rules live in [AGENTS.md](./AGENTS.md).

## What Is Included

- `tasks/` skeleton for durable task artifacts
- `tasks/USER_PREFERENCES.example.md` as a starting point for durable user defaults
- `data/projects/` skeleton for multi-task project records
- `data/local-projects.example.md` as a starting point for local repository/path indexes
- `AGENTS.md`, `.cursor/rules/`, and `CLAUDE.md` as one shared rule set for Codex, Cursor, and Claude Code
- `skills/task-creator/` for creating task directories and updating the index
- `skills/task-runner/` for parent-child CLI agent execution and optional multi-agent workflows
- `skills/task-artifacts/` for keeping task artifacts current during work
- `skills/project-organizer/` for durable project records
- `skills/repo-health/` for restore, publication, deliverables, and pre-push checks
- `skills/skill-maintainer/` for creating or changing skills
- `docs/` for architecture, task execution, Claude Code setup, and self-development workflows

## Quick Start

Create a virtual environment and install test dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

Create a task:

```bash
skills/task-creator/scripts/create_task.sh "Example task" "Try the task-agent workflow"
```

Run health checks:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py --allow-empty-tasks
PYTHONPATH=skills/task-runner/scripts .venv/bin/python -m pytest skills/task-runner/tests skills/repo-health/tests
```

Before pushing a source change from this workspace, run:

```bash
.venv/bin/python skills/repo-health/scripts/check_pre_push.py --remote origin
```

## Agent Entry Points

The same rules reach Codex, Cursor, and Claude Code without being copied. `AGENTS.md` holds the project rules, `.cursor/rules/*.mdc` hold the always-on rules, and `CLAUDE.md` imports both rather than restating them; `.claude/` contains only symlinks into the canonical files. Adding a Cursor rule means adding its `.claude/imports/` symlink and its `CLAUDE.md` import line — see [docs/claude-code-setup.md](./docs/claude-code-setup.md).

## Delegating To A Child Agent

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example
```

The child runner follows the parent CLI agent, so a Codex session delegates to Codex and a Claude session to Claude. Pass `--runner codex|claude|agent` to decide explicitly, or set `TASK_AGENT_CHILD_RUNNER`. Every run records which rule decided.

Access level is expressed once through `--sandbox-mode` (`read-only`, `workspace-write`, `danger-full-access`) and mapped per runner. `TASK_AGENT_WORKSPACE_ROOT` sets how far full access reaches; it defaults to the parent of this checkout.

`start` returns once the run is confirmed; the watcher and the child keep running in their own sessions, so closing the terminal does not end the work. Both processes are recorded by kernel start-time identity rather than by pid alone, so a recycled pid is never mistaken for the child. `reattach` restores a lost watcher and refuses when the pid was recycled or a watcher is already live.

## Dev-Pipeline Workflow

`--workflow dev-pipeline` runs a task through the standalone [dev-pipeline](https://github.com/) CLI, which drives an evidence-gated Codex or Claude owner session:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example \
  --workflow dev-pipeline --repo /path/to/target-repo
```

This workflow needs the `dev-pipeline` package, which is a separate project and is not on PyPI:

```bash
.venv/bin/pip install /path/to/dev-pipeline
```

The adapter is transport-neutral. It builds the owner instruction, calls the public CLI, validates the neutral lifecycle events it emits, and projects them into the task's `status.json`, `trace.md`, and `progress.json`. It binds no recipient and delivers no messages; `skills/task-runner/scripts/pipeline_notify.py` is the documented, deliberately inert seam where an application with a real transport attaches its own delivery and replay rules.

## Multi-Agent Workflow

`skills/task-runner/scripts/task_runner.py` supports `--workflow multi-agent-dev` for explicit team-of-agents development runs. By default use `--runner agent` (Cursor Agent CLI); pass `--runner codex` to run each role through Codex instead. This workflow does not support the Claude runner.

The workflow uses role prompts from `<workspace root>/agents` by default. If that checkout is missing, startup fails unless an agents repository URL is configured. Override with:

- `--agents-dir`
- `--agents-repo-url`
- `CODEX_MULTI_AGENT_PROMPTS_REPO`

Use `--model <model>` to pass a Codex model override through to every nested role run. Without an override, the workflow uses the current supported Codex default configured in the runner. If Codex rejects a stale model slug as unsupported, the workflow retries with its supported fallback sequence instead of treating that as a task blocker.

## Documentation

- [docs/architecture.md](./docs/architecture.md)
- [docs/task-execution.md](./docs/task-execution.md)
- [docs/self-development.md](./docs/self-development.md)

## License

Task Agent is released under the [MIT License](./LICENSE).

## Local State

`tasks/` and `data/` are durable local artifacts. This template tracks only skeleton and example files; real task history, `tasks/INDEX.md`, and reusable data should be backed up by your own local backup flow.
