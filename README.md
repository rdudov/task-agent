# Task Agent

Task Agent is a small, forkable workspace for task-first autonomous-agent workflows.

It is intentionally generic: no private task history, no local data, and no bundled personal integrations. Project-level operating rules live in [AGENTS.md](./AGENTS.md).

## What Is Included

- `tasks/` skeleton for durable task artifacts
- `data/projects/` skeleton for multi-task project records
- `skills/task-creator/` for creating task directories and updating the index
- `skills/task-runner/` for parent-child CLI agent execution and optional multi-agent workflows (Cursor Agent or Codex)
- `skills/project-organizer/` for durable project records
- `skills/repo-health/` for restore and publication checks
- `skills/skill-maintainer/` for creating or changing skills
- `docs/` for architecture, task execution, and self-development workflows

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
PYTHONPATH=skills/task-runner/scripts .venv/bin/python -m pytest skills/task-runner/tests
```

## Multi-Agent Workflow

`skills/task-runner/scripts/task_runner.py` supports `--workflow multi-agent-dev` for explicit team-of-agents development runs. By default use `--runner agent` (Cursor Agent CLI); pass `--runner codex` to run each role through Codex instead.

The workflow uses role prompts from `/opt/projects/agents` by default. If that checkout is missing, startup fails unless an agents repository URL is configured. Override with:

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

`tasks/` and `data/` are durable local artifacts. This template tracks only skeleton files; real task history and reusable data should be backed up by your own local backup flow.
