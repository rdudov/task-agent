---
name: task-runner
description: Use this skill when a substantial task should be delegated to a child CLI agent. It launches Codex or Cursor Agent against a task directory, writes progress scaffolding, provides status polling, and can run an explicit multi-agent development workflow (Cursor Agent by default).
---

# Task Runner

This skill launches a child CLI agent to execute a task from its task directory.

Use the standard single-child workflow by default. Use the multi-agent development workflow only when the user explicitly asks for a team-of-agents execution style.

## Artifacts

The runner expects:

- `task.md`
- `plan.md`

It creates or updates:

- `trace.md`
- `status.json`
- `.runner/prompt.txt`
- `.runner/runner.json`
- `.runner/runner.log`

The multi-agent workflow also creates `multi-agent/` by default.

## Commands

Start a standard Codex child:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner codex
```

Start the explicit multi-agent workflow (Cursor Agent CLI for each role):

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner agent --workflow multi-agent-dev
```

Use Codex for each pipeline role instead:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner codex --workflow multi-agent-dev
```

Resume an interrupted multi-agent run:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner agent --workflow multi-agent-dev --resume
```

Override the Codex model for every nested multi-agent role run:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner codex --workflow multi-agent-dev --model gpt-5.5
```

If Codex rejects a model as unsupported for the current account, treat that as recoverable runner configuration drift. Use the current supported model or let the runner fallback sequence choose one; do not report an unsupported stale model slug as a user-task blocker.

Check progress:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py status tasks/001-example
.venv/bin/python skills/task-runner/scripts/task_runner.py trace tasks/001-example
```

## Multi-Agent Prompt Repository

The multi-agent workflow uses role prompts from `/opt/projects/agents` by default.

If that prompt directory is missing, configure a clone source with `--agents-repo-url` or `CODEX_MULTI_AGENT_PROMPTS_REPO`. Use `--agents-dir` for a different checkout.

Startup verifies that the required role prompt files exist before running the first pipeline stage.

`--model <model>` is passed through to every nested Codex role run in the multi-agent workflow. Omit it to use the workflow's current Codex defaults and supported-model fallback.

## Completion Rules

- Keep task progress in `trace.md` and `status.json`.
- Follow `skills/task-artifacts/SKILL.md` for `verification.md`, `findings.md`, and plan checkpoints (mandatory before marking done).
- Store task-specific results in the task directory, not under `.runner/`.
- Preserve execution-critical user inputs in task artifacts.
- If external sources are used, write `findings.md` and `sources.md`.
- When `task_contract.json` is present, treat it as the execution contract for role prompts, review, and completion gates.
- Preserve the semantic target of the request. If the task names a reference behavior, artifact, provider, model, protocol feature, or runtime branch, implement and verify that named path directly instead of substituting a nearby effect unless the user or task contract explicitly accepts the substitution.
- Do not assume backward compatibility or a legacy fallback path unless the user request or project contract explicitly requires it.
- Mocked providers, fake models, and test-only harnesses are useful for unit coverage but are not sufficient acceptance evidence for production-reachable runtime branches by themselves.
- If behavior diverges by threshold, mode, provider, credential, feature flag, model path, transport, or fallback logic, each production-relevant branch touched by the task needs explicit validation evidence or a clearly recorded verification gap.
- If git-tracked source changes in a repository with a remote, commit and push after verification unless local-only work was requested or push is blocked.
- If task lifecycle, skill behavior, orchestration, restore, or resume behavior changes, update project docs in the same source change.
- If a source change was committed locally and then found to be wrong before it was pushed or shared, prefer removing that local commit with `git reset` or another explicit history rewrite after checking the working tree and commit graph. Use a revert commit only for pushed/shared history, ambiguous ownership, or explicit audit-history requirements.
- If the task-runner, prompt pipeline, or parent orchestration caused a material mistake, fix the active task and update the relevant skill/docs/rules in the same corrective pass so the failure mode is less likely to recur. If the prevention change is substantial or changes policy tradeoffs, record concrete options and ask the user before implementing the broad change.
- For services and runtimes, include a smoke check against the real entrypoint in the target launch mode.
