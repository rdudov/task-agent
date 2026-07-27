---
name: task-runner
description: Use this skill when a substantial task should be delegated to a child CLI agent. It launches Codex, Claude Code, or Cursor Agent against a task directory, resolving the child runner from the parent CLI agent, writes progress scaffolding, provides status polling, and can run an explicit multi-agent development workflow.
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

The child is asked to publish `progress.json` for long runs, and to place explicitly requested output files in `deliverables/` with `deliverables/manifest.json`.

The multi-agent workflow also creates `multi-agent/` by default.

## Runner Selection

The child runner follows the parent CLI agent. `task_runner.py` owns that decision, so callers that never read this file get the same resolution. The order is:

1. an explicit `--runner codex|claude|agent`, which always wins;
2. the `TASK_AGENT_CHILD_RUNNER` environment variable;
3. the nearest `claude` or `codex` process ancestor;
4. a single vendor's session markers in the environment (`CODEX_THREAD_ID`, `CLAUDECODE`), used only when no CLI ancestor is visible;
5. the documented fallback `codex`.

Ancestry outranks environment markers because both CLIs pass the other vendor's session variables to their children untouched, so a nested chain shows both. For the same reason every child is launched with all vendor session markers scrubbed from its environment; each CLI repopulates its own.

Ancestry is read from `/proc` where available and from `ps` otherwise. On a host with neither, detection degrades to environment markers and then to the fallback, and the recorded reason says so (`no_parent_signal:no_process_ancestry`). Detection never fails the launch.

The resolved runner and the rule that produced it are recorded as `runner` and `runner_resolution` in `.runner/runner.json`, `status.json`, and `trace.md`, so no run is ambiguous after the fact.

## Access Modes

Access level is expressed once, through `--sandbox-mode`, and mapped per runner:

| `--sandbox-mode` | Codex child | Claude child |
| --- | --- | --- |
| `workspace-write` (standard default) | `--sandbox workspace-write`, cwd repo root | `acceptEdits`, native sandbox writable only in cwd/temp |
| `danger-full-access` | `--sandbox danger-full-access`, cwd workspace root | permission bypass plus `--add-dir <workspace root>` |
| `read-only` | `--sandbox read-only` | only `Read`, `WebFetch`, `WebSearch`; `dontAsk`; no Bash |

The workspace root is the directory a full-access child may reach. It defaults to the parent of this checkout and is overridden with `TASK_AGENT_WORKSPACE_ROOT`. Nothing else in the runner hardcodes an absolute path.

Claude children keep the repository as their working directory so `CLAUDE.md`, and through it `AGENTS.md` and the always-on rules, load automatically.

Claude's restricted modes depend on its native OS sandbox, which is Linux-specific and needs `bubblewrap` and `socat`. The launcher checks both before starting and **fails closed** when they are missing or the host is not Linux; it never downgrades a requested boundary, because that would make task artifacts claim a confinement that never applied. On such a host, either run the child through Linux or choose `danger-full-access` deliberately.

Claude's Bash sandbox always grants cwd writes, so `read-only` deliberately omits Bash and every native file-writing tool; non-interactive `dontAsk` prevents permission escalation. `--add-dir` is discovery configuration, not confinement, and is used only for intentional full access. A root-launched session additionally sets Claude's documented weaker nested mode, which keeps bwrap filesystem confinement while exposing the host `/proc` view.

Restricted runs load only the project settings source, so `CLAUDE.md`, rules, and skills stay discoverable while user and local permission history does not load. They fail before launch if a checked-in `.claude/settings.json` contains hooks, other command-bearing configuration, plugins, non-read permission allow rules, added directories, unsandboxed excluded commands, extra write paths, filesystem disablement, proxy or network allowances, or Unix sockets.

`--model` applies only to the resolved runner, so a Codex model slug can never reach `claude`. A Claude child otherwise uses the account default unless `CLAUDE_CHILD_DEFAULT_MODEL` pins one.

## Commands

Let the parent be detected in normal use:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example
```

Start a standard Codex child:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner codex
```

Start a standard Claude child:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner claude
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

`status` includes a `progress` block when the child published a usable `progress.json`. See "Live Progress" below for what counts as usable.

## Live Progress

A long-running child should publish `progress.json` in its task directory:

```json
{
  "version": 1,
  "activity": "Reviewing module 3 of the migration",
  "updated_at": "2026-07-27T14:00:00+00:00",
  "recent_outcome": "Module 2 migrated, 14 call sites updated",
  "completed": 3,
  "total": 8,
  "unit": "modules"
}
```

`version`, `activity`, and `updated_at` are the substance. `recent_outcome` is optional. `completed`, `total`, and `unit` are optional **as a group**: publish all three or none, and only when the owner actually knows the bounds.

The reader enforces that contract rather than trusting it. A missing `version: 1` or a blank `activity` makes the whole file unusable, and a partial or incoherent count triple is reported as `counts_rejected` instead of being shown as half a measurement. This is deliberate: an inferred total is worse than no total, because it reads as a real estimate.

Startup and bookkeeping are not progress. "Preparing the task directory" tells a watching human nothing about the work.

## Multi-Agent Prompt Repository

The multi-agent workflow uses role prompts from `<workspace root>/agents` by default, where the workspace root is the parent of this checkout unless `TASK_AGENT_WORKSPACE_ROOT` says otherwise.

If that prompt directory is missing, configure a clone source with `--agents-repo-url` or `CODEX_MULTI_AGENT_PROMPTS_REPO`. Use `--agents-dir` for a different checkout.

The workflow rejects `--runner claude`. Its role prompts and model defaults are Codex- and Cursor-Agent-bound; running Claude roles is a code change, not a flag.

Startup verifies that the required role prompt files exist before running the first pipeline stage.

`--model <model>` is passed through to every nested Codex role run in the multi-agent workflow. Omit it to use the workflow's current Codex defaults and supported-model fallback.

## Completion Rules

- Keep task progress in `trace.md` and `status.json`, and publish substantive `progress.json` for a long run.
- Follow `skills/task-artifacts/SKILL.md` for `verification.md`, `findings.md`, and plan checkpoints (mandatory before marking done).
- Store task-specific results in the task directory, not under `.runner/`.
- Preserve execution-critical user inputs in task artifacts.
- If external sources are used, write `findings.md` and `sources.md`.
- Requested output files outrank generic task records: completion requires them in `deliverables/` and registered in `deliverables/manifest.json`. `findings.md` is not a substitute. Validate with `skills/repo-health/scripts/check_deliverables.py`.
- Rendering-dependent deliverables require real rendered visual inspection and internal evidence before completion; structural checks alone do not satisfy that gate.
- Verify artifact completeness against the source of truth before reporting success. A successful write or send proves delivery, not completeness.
- Stub-first work is allowed only for new seams or genuinely unavailable external systems. Do not stub over an existing exercisable production path, and do not credit a stub-only run with live evidence.
- Read the target repository's own operating context before raising language, syntax, build, or convention findings, and judge them against its declared runtime.
- When changed source is loaded by active local services or units, restart or reload them, verify they came up cleanly, and record that evidence before reporting done.
- When `task_contract.json` is present, treat it as the execution contract for role prompts, review, and completion gates.
- Preserve the semantic target of the request. If the task names a reference behavior, artifact, provider, model, protocol feature, or runtime branch, implement and verify that named path directly instead of substituting a nearby effect unless the user or task contract explicitly accepts the substitution.
- Do not assume backward compatibility or a legacy fallback path unless the user request or project contract explicitly requires it.
- Mocked providers, fake models, and test-only harnesses are useful for unit coverage but are not sufficient acceptance evidence for production-reachable runtime branches by themselves.
- If behavior diverges by threshold, mode, provider, credential, feature flag, model path, transport, or fallback logic, each production-relevant branch touched by the task needs explicit validation evidence or a clearly recorded verification gap.
- If git-tracked source changes in a repository with a remote, fetch and sync the base branch before branching, commit after verification, run the pre-push leak check, then push unless local-only work was requested or push is blocked.
- If task lifecycle, skill behavior, orchestration, restore, or resume behavior changes, update project docs in the same source change.
- If a source change was committed locally and then found to be wrong before it was pushed or shared, prefer removing that local commit with `git reset` or another explicit history rewrite after checking the working tree and commit graph. Use a revert commit only for pushed/shared history, ambiguous ownership, or explicit audit-history requirements.
- If the task-runner, prompt pipeline, or parent orchestration caused a material mistake, fix the active task and update the relevant skill/docs/rules in the same corrective pass so the failure mode is less likely to recur. If the prevention change is substantial or changes policy tradeoffs, record concrete options and ask the user before implementing the broad change.
- For services and runtimes, include a smoke check against the real entrypoint in the target launch mode.
- A child that exits non-zero before writing a terminal state does not leave `running` behind: the runner records terminal `failed` with the child exit code in `status.json` and `trace.md`. Treat that as a real failure, not as an unfinished run.
