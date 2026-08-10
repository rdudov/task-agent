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

## Supervision

A launched run is detached by design. `start` prepares the artifacts, spawns a watcher in a separate session, waits only for the watcher's startup record, and returns; the watcher spawns the child in a session of its own. Nothing in the chain stays in the caller's process group, so the run survives the terminal that began it. When the host systemd manager is reachable, the watcher also runs in its own transient scope; otherwise `.runner/runner.json` records that durability is limited to the caller's cgroup.

Because the run outlives its initiator, a pid alone is not enough to identify it later. The runner records a kernel start-time identity for both the child and the watcher in `.runner/runner.json`, and treats a pid whose identity no longer matches as a different process: `status` reports how each liveness verdict was reached, `stop` refuses to signal an unproven pid, and `reattach` refuses to supervise one. Where the host cannot produce identities, pid-only checks are marked as such and `reattach` fails closed rather than guessing. See `skills/task-runner/SKILL.md` for the per-command behavior.

A watcher that is recovered rather than original cannot read the child's exit code. It observes liveness and then reads the terminal state the child recorded; a child that disappears without one is recorded as failed, never as done.

Launch ownership is serialized. A second start for a task with an identity-bound
live child or watcher is refused before any metadata is replaced, because two
live writers make progress attribution and later stop/reattach unsafe.
New runs also bind that evidence to their PID namespace. From another namespace,
an absent host PID is unobservable rather than dead, so status reports unknown
and start/stop/reattach fail closed.

Liveness itself degrades rather than disappearing. `live_run_processes()`
accepts pid-only evidence where the host cannot produce kernel identities,
because the callers acting on it are the ones refusing a second run and an
overlapping repository write — and requiring proof on a host without `/proc`
would report every live run as dead, so the hosts unable to detect a concurrent
run would be the hosts that admit one. Each answer names the evidence it rests
on. `reattach` still insists on proof, because refusing a child that only looks
alive is its entire purpose.

`--repo <path>` is runner-neutral. In the standard workflow it becomes an
explicit Codex/Claude access root or an additive Cursor Agent workspace root;
write modes perform an exclusive random-file create/delete probe before launch
and record the grant. Cursor retains task-agent as its primary workspace. In
the dev-pipeline workflow the same path is the core owner's target repository.

A write-mode launch is additionally admitted against the target repository, so
two tasks never write one working tree at the same time and a change nobody has
reviewed does not get built on. `skills/task-runner/SKILL.md` owns the rules and
the ledger format.

## Task Phases

A user goal keeps one task number for its whole life. Review is a phase of that
task and so is the rework a review asks for — under both profiles, in one
directory. A `dev-pipeline` run's phases come from the neutral lifecycle events;
a `standard` run's come from what the run was asked to do and what the task has
already been through, so both present the same vocabulary to a reader.

`phases.json` holds the current phase and an append-only history with the cause
of each transition. `task_engine.py phases TASK` prints it, and
`task_engine.py state TASK` returns it alongside identity, completion readiness,
actuality and supervision in one JSON document — the surface a downstream
consumer uses instead of importing internals. `skills/task-runner/SKILL.md` owns
the vocabulary, the event mappings and the actuality threshold.

## Dev-Pipeline Workflow

`--workflow dev-pipeline` delegates a task to the standalone `dev-pipeline` CLI, which drives an evidence-gated Codex or Claude owner session against a target repository:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example \
  --workflow dev-pipeline --repo /path/to/target-repo
```

The runner decides nothing about the pipeline itself. `skills/task-runner/scripts/dev_pipeline_adapter.py` owns the integration: it renders the owner instruction from `task.md`, calls the public CLI, validates the neutral lifecycle events the core emits, and projects them into the task's `status.json`, `trace.md`, and `progress.json`. The core interprets owner-runtime behavior; the adapter only projects what the core reports.

The workflow states its own outcome through those events, so a subprocess exiting cleanly is not a completion. A dev-pipeline child that ends without a terminal event is recorded as failed. Standard and dev-pipeline finalizers share one completion decision: YAML frontmatter must be `completed`, `plan.md` must have no `[pending]` or `[in_progress]`, every required evidence gate's latest section must record `OK`, `PASS`, or `PASSED`, any required author verdict must be unambiguous, and enforced policy families need an approved digest-bound review.

The owner instruction states these conditions explicitly. Generate the bounded
policy review over a final committed candidate with `task_runner.py
review-candidate TASK --repo REPOSITORY`; the subject binds the effective
contract and candidate digest, so a stale or readability-only approval cannot
close the task.

The adapter carries no transport: no destination, no recipient binding, no delivery deduplication. Those belong to an application that owns a transport, and `skills/task-runner/scripts/pipeline_notify.py` is the documented seam where it attaches. In this template that seam is inert by design.

This workflow requires the separate `dev-pipeline` package; see `skills/task-runner/SKILL.md` for the install step and the per-artifact projection rules.

The dependency comes from the public `rdudov/dev-pipeline` repository and is
pinned by commit in both requirements files. The normal README installation is
therefore sufficient on a fresh Cursor machine; a local checkout is optional.

When `task_contract.json` is present, an orchestrated workflow should carry it into the work as a task execution contract overlay instead of trusting stage documents alone to preserve hard constraints. Review and final completion should validate against that contract, not only against free-text summaries.

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

For read-only reviews, the subject stays outside every writable root while the
reviewer's own task directory and `.state/` remain writable. Codex implements
this as a scoped workspace-write sandbox rooted at the notebook; Claude uses
`dontAsk`, sandboxed Bash, and an explicit allow-list. This boundary permits
search, tests, `findings.md`, and canonical task closure without granting edits
to the reviewed repository.

## Progress Artifacts

Operational detail: `skills/task-artifacts/SKILL.md` (checkpoints, `verification.md`, completion checklist).

Recommended artifacts:

- `trace.md`
- `status.json`
- `.runner/runner.json`
- `.runner/runner.log`
- `dev-pipeline/` — lifecycle state and projected events for a dev-pipeline run
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
  "schema_version": 1,
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
