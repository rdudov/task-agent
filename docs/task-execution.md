# Task Execution

This document describes the parent-child execution model for non-trivial tasks.

## Recommended Model

1. Apply the substantial-request check from `AGENTS.md`. Default to a task directory unless the request is clearly trivial or the user explicitly opts out.
2. Before broad search or live checks, inspect existing durable context: `tasks/INDEX.md`, related task artifacts, and local project/path indexes based on `data/local-projects.example.md`.
3. Before file edits, shell-driven implementation, delegation, or live verification, create or update the task directory via `skills/task-creator/`.
4. Ensure `task.md` and `plan.md` preserve enough context for independent execution.
5. Add `task_contract.json` for non-negotiable constraints, forbidden substitutions, or mandatory live verification gates.
6. Launch a child CLI agent when substantial work should stay out of the parent conversation.
7. Require the child to write progress and outputs back into the same task directory.
8. Monitor task artifacts instead of waiting silently.
9. Before completion, promote reusable lookup knowledge, commands, limits, or workflow details to an index, skill, or rule; keep one-off results in the task directory.

## Multi-Agent Workflow

When the user explicitly asks to solve a task with a team of agents, the parent may use:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner agent --workflow multi-agent-dev
```

Each pipeline role runs through the Cursor Agent CLI (`agent`). Use `--runner codex` when every role should run through Codex instead.

The workflow uses an external role-prompt repository. By default it expects `<workspace root>/agents`, where the workspace root is the parent of this checkout unless `TASK_AGENT_WORKSPACE_ROOT` says otherwise. If that checkout is missing, configure an agents repository URL with `--agents-repo-url` or `CODEX_MULTI_AGENT_PROMPTS_REPO`. Use `--agents-dir` for a different local checkout.

The multi-agent workflow rejects `--runner claude`; its role prompts and model defaults are Codex- and Cursor-Agent-bound. The standard single-child workflow supports all three runners.

Startup verifies that all required role prompt files exist before the first pipeline stage proceeds.

If the parent starts the multi-agent workflow with `--model <model>`, that model override should be passed to every nested Codex role run. Without an explicit override, the orchestrator should use current supported Codex model defaults. Stale role defaults are an orchestration defect, not a blocker for the user task: when Codex reports that a model is unsupported, the runner should retry with the current recommended model or a documented supported fallback and continue from existing artifacts.

When `task_contract.json` is present, the multi-agent orchestrator should inject it into every role prompt as a task execution contract overlay instead of trusting stage documents alone to preserve hard constraints. Review and final completion should validate against that contract, not only against free-text stage summaries.

Analysis should stop on unresolved task semantics before architecture begins. If the task still has explicit open questions about fallback behavior, backward compatibility, migration scope, runtime failure mode, or rollout source of truth, the analyst should surface them as blocking questions and the pipeline should wait for clarification.

Agents should not assume backward compatibility, legacy fallback branches, or "keep the old behavior too" unless the user request or project contract explicitly requires that target.

Implementation should preserve the semantic target of the request. If the task names a reference behavior, artifact, model, provider, protocol feature, or runtime branch, the solution should use and verify that named path directly instead of replacing it with a nearby effect that happens to look similar. Any substitution requires explicit user acceptance or task-contract approval; otherwise it is a blocker or an unverified deviation.

If an interruption or incorrect result was caused by an agent or runner mistake, such as an unsupported hard-coded model, wrong sandbox, or wrong git operation, the parent should perform a short corrective review before resuming. The review should identify the cause, repair the active task state, and update the relevant runner, skill, docs, or project rules unless that prevention work is too broad; in that case the parent should present options to the user.

## Verification

Substantial implementation work should keep at least one no-mock end-to-end verification path for the primary behavior being changed.

For services, APIs, daemons, workers, and other runtime processes, completion should include a smoke check against the actual runtime entrypoint in its target launch mode.

One smoke on the default path is not enough when production behavior diverges across runtime branches. If execution changes by threshold, mode, provider, credential, feature flag, model variant, transport, or fallback branch, each production-relevant branch touched by the task should be validated at the real runtime boundary or explicitly left open as an unverified risk.

Mocked or fake-model coverage of a production branch is useful for unit isolation but does not count as branch validation by itself. Review should treat "tested only with a fake provider/model/runtime" as a verification gap when that branch can execute in production.

Test data across the verification stack should be representative of real user or system inputs for the behavior being validated. A degenerate fixture that only proves transport or parser compatibility is weak evidence when more realistic samples are practical.

Examples of branch-specific checks that should not be skipped:

- mode-specific behavior such as default vs longform vs fallback paths
- provider-specific behavior when routing or credentials differ
- feature-flagged branches that are intended to be enabled in production
- secret/token-gated branches whose dependencies are absent from the default happy path

Stub-first work is allowed only for a newly introduced seam or a genuinely unavailable external system. Replacing an existing, exercisable production integration with a stub does not produce evidence about that integration, so a stub-only run never satisfies a live-evidence gate.

When changed source is loaded by an active local service, daemon, worker, or unit, the change is not in effect until those units are restarted or reloaded. Completion includes doing that, confirming they came up with fresh start timestamps, checking recent logs, and recording the evidence — or stating explicitly that it was deferred.

When a deliverable's correctness depends on how it renders, render it and look at the result. Archive validity, DOM parsing, and text extraction describe the file, not the page a person will see: they cannot detect clipped text, overlapping elements, a substituted font, or a broken image. `skills/task-artifacts/SKILL.md` describes the required coverage per format.

Before reporting success, check that the artifact is complete against its source of truth — size, line count, boundaries, truncation markers. A successful write or send proves delivery, not completeness.

## Progress Artifacts

Operational detail: `skills/task-artifacts/SKILL.md` (checkpoints, `verification.md`, completion checklist).

Recommended artifacts:

- `trace.md`
- `status.json`
- `.runner/runner.json`
- `.runner/runner.log`
- `multi-agent/`
- `task_contract.json`
- `verification.md` — live smokes and contract gates (redacted)
- `findings.md`
- `sources.md`
- `progress.json` — substantive live progress during a long run
- `deliverables/` and `deliverables/manifest.json` — explicitly requested output files

Reusable local lookup context, such as repositories, important paths, recurring commands, or where prior artifacts live, should be recorded under `data/`. This template includes `data/local-projects.example.md` as a generic starting point.

Durable answers to "how does the user want unspecified things done" belong in `tasks/USER_PREFERENCES.md`, not in a task directory. Write there only from an explicit reusable instruction, cite the task it came from, and let the current request override it. A one-off requirement is not a preference.

Suggested `progress.json` for a long run:

```json
{
  "version": 1,
  "activity": "Migrating module 3 of the payment adapter",
  "updated_at": "2026-04-03T12:00:00Z",
  "recent_outcome": "Module 2 migrated, 14 call sites updated",
  "completed": 3,
  "total": 8,
  "unit": "modules"
}
```

`completed`, `total`, and `unit` are published together or not at all, and only when the owner knows the bounds. A reader must never infer a missing total: a made-up denominator reads as a real estimate and quietly misleads whoever is watching. `task_runner.py status` validates this and reports an incomplete triple as `counts_rejected` rather than showing half a measurement.

Suggested `status.json`:

```json
{
  "state": "in_progress",
  "current_step": "Running verification",
  "updated_at": "2026-04-03T12:00:00Z"
}
```

## Source Publication

When a child changes git-tracked source in a repository with a remote, it should commit and push after verification unless the task explicitly requires local-only work or push is blocked. Any unpushed source changes should be recorded with the reason and current repository state.

Before creating a branch from a remote-tracked base, fetch the remote and fast-forward the base branch. Before pushing, fetch again, rebase or merge onto the latest target branch according to the repository policy, run relevant tests, run the pre-push leak check, and review the outgoing diff. Use force push only when the task explicitly requires rewriting a branch and prefer `--force-with-lease`.

When the wrong change exists only in local, unpushed git history, prefer removing it from history instead of adding a compensating revert commit. Use `git reset` or another explicit history-rewrite operation after checking that no user-owned or dependent commits would be lost. Record the previous HEAD, target HEAD, and working-tree status in the task trace. Use a revert commit for pushed/shared history, ambiguous ownership, or when the user explicitly asks for audit-preserving history.

If the child changes task lifecycle, task artifact structure, skill discovery or execution, agent orchestration, restore behavior, or resume behavior, it should update relevant project docs in the same source change.

If the child or parent caused a material mistake during the task, the final artifacts should include the corrective action and the prevention change, or a user-facing choice when prevention requires a larger design decision.
