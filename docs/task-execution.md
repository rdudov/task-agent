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

An installation binds automatic review/rework by passing both
`--assurance-config` and `--review-packet` to the ordinary task-runner `start`
entrypoint. Both paths survive detached watcher construction and reach the same
`dev-pipeline owner` process. Task-agent never selects or substitutes a reviewer
itself; it does refuse a material dev-pipeline launch whose assurance would
review with a family other than the one admission bound, or with nobody at all.

A launch is also admitted against the review it will need. Material work — a
write grant, a non-read-only sandbox, a delivery workflow, a gated contract,
declared review gates, registered deliverables — is refused before the author
starts unless an independent provider family can be bound as its reviewer, and
the author is never bound to review itself. The narrow exception is a launch
declared `review_policy.work_class: "read_only_lookup"` in `task_contract.json`
that observably does none of those things; a declaration any observation
contradicts is a mislabel and the launch stays reviewable. The bound reviewer
then governs the run and its acceptance rather than only being recorded: the
work is not accepted as complete until that family has approved what is there
now. Nothing here counts
rework rounds — review and rework are phases of one number, continued until the
work is accepted. `skills/task-runner/SKILL.md` owns the rules, the record
format, and the repeated-finding quality warning.

A write-mode launch is additionally admitted against the target repository, so
two tasks never write one working tree at the same time and a change nobody has
reviewed does not get built on. `skills/task-runner/SKILL.md` owns the rules and
the ledger format. Successful completion is preserved in that append-only ledger
against the exact write-scope run IDs it covered, including bounded recovery of
a terminal scope whose watcher did not close it. Legacy backfill additionally
requires the matching controller completion event and any review evidence the
current contract requires. The shared completion owner revalidates historical
policy review from immutable Git objects plus its complete context, diagnostics,
and decision envelope; write admission does not parse review claims itself. An
earlier ungated terminal state therefore cannot satisfy a later review gate. A
legitimate later commit can stale a current review packet without retroactively
turning the older completed task into an admission blocker; later rework has new
run IDs and must close its own gates. The migration receipt names the validated
candidate head and accepts only scope results whose Git heads are ancestors of
that candidate; it never clears a later same-task write. Historical validation
uses the stored Git objects and envelope directly, so unrelated current dirt or
a later dependency checkout does not become retroactive evidence about task A.

## Task Phases

A user goal keeps one task number for its whole life. Review is a phase of that
task and so is the rework a review asks for — under both profiles, in one
directory. A `dev-pipeline` run's phases come from the neutral lifecycle events;
a `standard` run's come from what the run was asked to do and what the task has
already been through, so both present the same vocabulary to a reader.

`phases.json` holds the current phase and an append-only history with the cause
of each transition. `task_engine.py phases TASK` prints it, and
`task_engine.py state TASK` returns it alongside identity, completion readiness,
actuality, supervision, and the state of the independent review the launch was
admitted with, in one JSON document — the surface a downstream
consumer uses instead of importing internals. `skills/task-runner/SKILL.md` owns
the vocabulary, the event mappings and the actuality threshold.

An assurance gate is not optional for material work. The launcher binds the
installation strategy before the author starts, and that binding decides both
how the run is checked and whether it can be accepted. With no configuration,
the historical Codex↔Claude cross-provider pairing remains mandatory. Explicit
`isolated_same_provider` requires a fresh read-only review session of the same
provider; explicit `live_acceptance_only` requires every named live scenario and
records no model verdict. A missing configured provider stops without fallback.
A material model-reviewed `standard` author run therefore ends `blocked` until
the review runs as a phase of the same number:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py review tasks/001-example
```

That blocked state is canonical metadata, not only a runtime annotation: if an
author prematurely wrote `completed`, the finalizer restores `blocked` through
`tasks_index.py` whenever the run is refused, failed, blocked, or paused. A new
admitted review process begins with no stale canonical `Verdict:` lines in
`findings.md`; prior decisions remain in the append-only rounds ledger, and a
launch failure restores the prior line because no reviewer started. After any
installation-specific quota disposition is handled, a clean review exit records
the new round. If it is approved and every other completion gate passes, the
same finalizer uses `tasks_index.py` to set `completed`, rechecks the full
predicate, and closes `status.json` and the phase without a separate bookkeeping
run.

Rounds are appended to `reviews/rounds.jsonl` without any ceiling, and the
bindings to `reviews/admissions.jsonl`. An unapproved round refuses acceptance
and authorizes the next round; nothing counts down, in this project or in the
dev-pipeline revision it pins.

These remaining records are deliberately narrow: admissions preserve the
author/reviewer promise across phases, rounds preserve the latest decision, and
phase history detects author work after approval. The launcher owns all three;
an installation adds transport and resource policy, not another pairing record.

A binding reaches that ledger only from a launch that started an author. A
launch refused before its child — by the application launch policy, by a watcher
that could not be spawned, or by a watcher that refused before its own child —
either never appends its binding or withdraws it with an `annulled_admission`
entry, so the family that actually wrote the work stays the number's author and
the reviewer bound to it stays the family that may review it.

A committed binding is outstanding, in `.runner/review-admission-commitment.json`,
until the process that spawns the child confirms that an author started. Any
process that can still end the launch withdraws it — the parent through its own
abort, the detached watcher through its pre-child failure, each settling only the
launch token it belongs to — and while the commitment is outstanding the binding
it made is not read as the number's binding, so a launch whose parent and watcher
are both gone is answered correctly with nobody left to act.

## Dev-Pipeline Workflow

`--workflow dev-pipeline` delegates a task to the standalone `dev-pipeline` CLI, which drives an evidence-gated Codex or Claude owner session against a target repository:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example \
  --workflow dev-pipeline --repo /path/to/target-repo
```

The runner decides nothing about the pipeline itself. `skills/task-runner/scripts/dev_pipeline_adapter.py` owns the integration: it renders the owner instruction from `task.md`, calls the public CLI, validates the neutral lifecycle events the core emits, and projects them into the task's `status.json`, `trace.md`, and `progress.json`. The core interprets owner-runtime behavior; the adapter only projects what the core reports.

The workflow states its own outcome through those events, so a subprocess exiting cleanly is not a completion. A dev-pipeline child that ends without a terminal event is recorded as failed; a validated quota-wait event is a durable pause and is not rewritten as that failure. Standard and dev-pipeline finalizers share the durable status, plan, live-evidence, explicit-verdict, and admitted-independent-review checks, even when the child wrote `completed` itself. Enforced policy families additionally need an approved digest-bound review for dev-pipeline; standard tasks do not manufacture that profile-specific review surface. That reviewer decides only the two prose policy families against the exact candidate. Live-evidence gates remain owned by the completion predicate, so a pre-terminal policy reviewer neither requires a future delivery/completion receipt nor converts its honest pending state into a policy-family failure.

For a registered completion-preparation application, a precondition or
transport refusal is projected with its original reason and an
`automatic_finalization` marker. The subsequent full predicate must not mask it
with the still-deferred task status. This lets an installation keep recovery
with the continuous owner instead of instructing the user to close canonical
metadata by hand.

The detached watcher's terminal `outcome` is derived after that finalization:
`succeeded` requires an accepted completed state, a clean exit refused by the
durable gate is `rejected_completion_contract`, and a validated quota pause is
`waiting_for_quota`. Exit code zero alone is never recorded as success.

The owner instruction states these conditions explicitly. Generate the bounded
policy review over a final committed candidate with `task_runner.py
review-candidate TASK --repo REPOSITORY`; the subject binds the effective
contract and candidate digest, so a stale or readability-only approval cannot
close the task.

The adapter itself carries no transport policy. A caller registers application
API v1 with `--application module:attribute` and supplies an opaque
`--destination`; notable lifecycle events then reach the application's
`deliver_event` method. Review start, required rework, review refusal, and exact
quota waiting are notable transitions rather than silent internal events. The public template's default application is inert.
Recipient binding, deduplication, replay, and document receipts remain owned by
the installation, while the public adapter still validates and orders the
events before offering them. On adapter startup `recover_transport` receives
the durable validated event-log path and the active attempt identity, allowing
the installation to reconcile its own receipt journal without the engine
inventing replay safety. Older attempts remain auditable but are outside the
notification replay boundary, so their terminal outcome cannot be reclassified
from the current task status.

The same registered application controls the named installation boundaries for
standard runs:

```bash
task-agent start /absolute/task --workflow standard --runner claude \
  --operation resume --application my_app.task_agent:adapter \
  --destination "$INSTALLATION_DESTINATION" --memory-limit 4G
```

`standard_session` receives the prior non-secret state and returns the exact
native CLI arguments for `start`, `resume`, or `retry`; the runner persists and
reuses those arguments across its detached watcher boundary. The watcher
verifies the application registration, operation, destination binding, and
prepared session before it launches the child. If any required value is absent
or differs from the parent record, the run terminates as a visible launch
failure rather than silently starting a new native session.
`standard_run_finished` receives the supervised exit and log, and may turn an
exact provider reset into a durable `waiting_for_quota` disposition after
arming the installation scheduler. It does not replace the engine's run
ownership or completion decision. Raw
destinations are not written to runner metadata or recorded commands.
Consequently, recovery after the original command has exited must obtain the
recipient from installation-owned state; the engine supplies the durable event
log and its persisted destination digest, not the raw destination.

This workflow requires the separate `dev-pipeline` package; see `skills/task-runner/SKILL.md` for the install step and the per-artifact projection rules.

The adapter cursor is attempt-scoped and sequence-strict. It accepts a new run
identity only on a lifecycle boundary the public core defines—owner
start/resume, independent review, same-session rework, live acceptance, or an
escalated phase blocker—and rejects a foreign run identity on ordinary events.
This lets automatic cross-provider phases remain one attempt without weakening
the projector's identity check.
Before a restarted adapter invokes the core, it replays that state directory's
authoritative lifecycle ledger through the same cursor. This closes a crash
between the core's durable append and adapter projection without synthesizing
events or skipping delivery; repeats remain idempotent by event ID.

A deliberate runner stop is also durable. For dev-pipeline runs, `stop` invokes
the core's `handoff request-stop` against the recorded state directory before it
signals the process group. A later ordinary resume may reopen only that exact
claimed review or rework phase. Failure to write the marker refuses the stop,
while unexplained claimant loss keeps the public core's user-decision blocker.

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
an outer bubblewrap mount namespace as the sole filesystem boundary: Claude's
nested sandbox is disabled, its Read/Grep/Glob/Web/Bash/Write/Edit tools run with
non-interactive permission bypass, the host tree is read-only, and only the
notebook, task index, `/tmp`, and Claude-owned runtime storage are rebound
writable. Network and host-process visibility are unchanged. Tests that write
runtime state must redirect it to the notebook or `/tmp`; live host state stays
read-only. The admitted-review prompt explicitly names the reviewer role,
subject, author, target repository, and required single-line verdict.

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
