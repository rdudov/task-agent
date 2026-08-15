---
name: task-runner
description: Use this skill when a substantial task should be delegated to a child CLI agent. It launches Codex, Claude Code, or Cursor Agent against a task directory, resolving the child runner from the parent CLI agent, writes progress scaffolding, supervises the detached run, provides status polling, and can run a task through the dev-pipeline workflow.
---

# Task Runner

This skill launches a child CLI agent to execute a task from its task directory.

Use the standard single-child workflow by default. Use the dev-pipeline workflow when a task should run through the evidence-gated dev-pipeline lifecycle instead.

Before implementation, use the canonical no-code-first order: do nothing;
remove or disable; configure or reuse; simplify; only then add the smallest
necessary code and state why the observed gap required it. Independent review
treats avoidable code or needless complexity as a defect.

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
- `.runner/review-admission.json` for the current launch,
  `.runner/review-admission-commitment.json` while a committed binding is waiting
  for its author to start, and `reviews/admissions.jsonl` for every admission
  this number has made
- `reviews/rounds.jsonl` and `reviews/infrastructure-obligations.jsonl`, once a
  review round or a review outage happens

The child is asked to publish `progress.json` for long runs, and to place explicitly requested output files in `deliverables/` with `deliverables/manifest.json`.

The dev-pipeline workflow also creates `dev-pipeline/`.

An application that already supervises the entire worker service or container
may use `start --foreground` and `review --foreground`. These forms keep the
same admission, binding, phases, review rounds and completion decision, but wait
for the child in the caller process instead of detaching a watcher that the
outer container would terminate. The recorded supervision durability is
`caller_owned`; foreground never supplies a missing assurance strategy or
reviewer.

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
| `read-only` | task directory writable through a scoped `workspace-write`; subject remains outside writable roots | `Read`, `Grep`, `Glob`, and sandboxed Bash inside an outer read-only mount namespace; only the task directory, `.state/`, and Claude runtime storage are writable |

The workspace root is the directory a full-access child may reach. It defaults to the parent of this checkout and is overridden with `TASK_AGENT_WORKSPACE_ROOT`. Nothing else in the runner hardcodes an absolute path.

Workspace-write and full-access Claude children keep the repository as their
working directory. Read-only reviews instead use their task notebook as cwd;
Claude discovers applicable `CLAUDE.md`/`AGENTS.md` files from that nested path,
while the prompt carries the reviewer role and subject explicitly.

Claude's restricted modes depend on its native OS sandbox, which is Linux-specific and needs `bubblewrap` and `socat`. The launcher checks both before starting and **fails closed** when they are missing or the host is not Linux; it never downgrades a requested boundary, because that would make task artifacts claim a confinement that never applied. On such a host, either run the child through Linux or choose `danger-full-access` deliberately.

Read-only review protects the subject, not the reviewer's notebook. Codex maps
the mode to a workspace-write sandbox rooted at the task directory. Claude
disables its nested sandbox, exposes Read/Grep/Glob/Web/Bash/Write/Edit with
non-interactive permission bypass, and relies on an outer bubblewrap namespace
whose host mount is read-only; only the notebook, task index, temporary space,
and Claude-owned runtime storage are rebound writable. The filesystem boundary
does not restrict network access, host-process visibility, or signals. It lets
the reviewer search, write `findings.md`, close its own task, and run tests that
do not write live host state; such tests must redirect their runtime state to
the notebook or `/tmp`. An admitted review prompt
names the reviewer role, subject, author, repository, and required verdict so the
child reviews rather than re-executing the author task.

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

To work in another repository, add `--repo /path/to/target-repo`. Standard
Codex and Claude receive a narrow additional root; Cursor Agent receives it as
an additional root while retaining task-agent as its primary workspace. Write
modes verify the target with an exclusive random-file create/delete probe
before spawn, and the resolved grant is recorded in `.runner/runner.json`.

Start a standard Claude child:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example --runner claude
```

Run a task through the dev-pipeline workflow:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example \
  --workflow dev-pipeline --repo /path/to/target-repo
```

Name the reviewing family explicitly when the launch should not take the first
independent one installed:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example \
  --runner claude --reviewer-runner codex
```

If a runner rejects a model as unsupported for the current account, treat that as recoverable runner configuration drift rather than a user-task blocker: use a currently supported model.

Check progress:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py status tasks/001-example
.venv/bin/python skills/task-runner/scripts/task_runner.py trace tasks/001-example
```

`status` includes a `progress` block when the child published a usable `progress.json`. See "Live Progress" below for what counts as usable.

Ask the public engine what a task is and where it stands:

```bash
.venv/bin/python skills/task-runner/scripts/task_engine.py state tasks/001-example
.venv/bin/python skills/task-runner/scripts/task_engine.py phases tasks/001-example
.venv/bin/python skills/task-runner/scripts/task_engine.py actuality tasks/001-example
.venv/bin/python skills/task-runner/scripts/task_engine.py admission tasks/001-example --repo /path/to/target-repo
```

`task_engine.py` is the surface for anything downstream — a product layer, a
transport adapter, another installation. It composes the existing owners and
answers in JSON, so a consumer never imports a helper out of `task_runner.py`
and never breaks when one is renamed. `state` returns identity, phase, phase
sequence, contract gate status, completion readiness, actuality, supervision and
any outstanding Git write result in one document.

Restore supervision of a child whose watcher was lost:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py reattach tasks/001-example
```

## Process Supervision

`start` returns as soon as the run is confirmed, and the work continues without it. The caller spawns a watcher in its own session; the watcher spawns the child in another. Neither is in the caller's process group, so closing the terminal that started the run does not end it.

A recorded pid is not, by itself, a handle on a process: the kernel recycles pids, so a stale pid can name something unrelated. `process_identity(pid)` pins the specific incarnation by hashing the process start-time tick from `/proc/<pid>/stat`. Command text takes no part in it, because a process may rewrite its own argv after launch — the Node-based Codex CLI does. Both identities are recorded in `.runner/runner.json` as `process_identity` and `watcher_process_identity`.

`status` reports `process_alive` together with `process_alive_source`, which says how the verdict was reached: `identity_match` is proof, while a `pid_only_*` source is a weaker guess. `stop` refuses outright on `identity_mismatch` rather than signalling a process group that may no longer be the child's.

**Degradation.** Process identity reads `/proc`, which is Linux-specific. Where it is unavailable, `process_identity()` returns None for every pid; `process_identity_available()` tells that host apart from a dead process, and callers that can tolerate it fall back to pid-only liveness and say so in `process_alive_source`. `reattach` does not tolerate it and fails closed, because its whole job is refusing a child that only looks alive.

`live_run_processes()` does tolerate it, and must. It answers "is anything still running for this task", and the caller acting on that answer is the one refusing a second run and the one refusing an overlapping repository write. Requiring proof on a host without `/proc` — macOS — would report every live run as dead, so precisely the hosts that cannot detect a concurrent run would be the hosts that admit one. Each returned process carries the `evidence` its verdict rests on, and refusal messages name it, so a weaker host is visible rather than silently equivalent.

`reattach` restores a watcher for a child that is still running. It refuses when:

- the recorded pid is alive but its identity no longer matches, which is what a recycled pid looks like;
- a watcher recorded for the task is still live, so a second one would duplicate supervision;
- the run predates identity tracking, or the host cannot produce identities at all.

A recovered watcher is not the child's parent and cannot read an exit code. It observes the identity until it disappears, then reads the terminal state the child was supposed to record. If there is none, it writes terminal `failed` and says so in `trace.md`; a child vanishing is not a completion.

The runner serializes launch ownership and refuses a second live run before it
can overwrite the first run's identities or progress baseline. On hosts where a
systemd manager is reachable, the watcher starts in its own transient scope so
stopping a parent service does not kill the task; the recorded
`supervision_boundary` states whether this stronger boundary was available.

## Live Progress

A long-running child should publish `progress.json` in its task directory:

```json
{
  "schema_version": 1,
  "activity": "Reviewing module 3 of the migration",
  "updated_at": "2026-07-27T14:00:00+00:00",
  "recent_outcome": "Module 2 migrated, 14 call sites updated",
  "completed": 3,
  "total": 8,
  "unit": "modules"
}
```

`schema_version`, `activity`, and `updated_at` are all required. `recent_outcome` is optional. `completed`, `total`, and `unit` are optional **as a group**: publish all three or none, and only when the owner actually knows the bounds. The key is `schema_version`, matching the contract this template's upstream and the `dev-pipeline` owner protocol already use, so the same producer satisfies both.

The reader enforces that contract rather than trusting it. A wrong `schema_version`, a blank `activity`, a missing `updated_at`, or a file that does not parse all make the progress unusable and yield nothing rather than a partial reading. A partial or incoherent count triple is reported as `counts_rejected` instead of being shown as half a measurement, and a boolean is never accepted as a count. This is deliberate: an inferred or fake total is worse than no total, because it reads as a real estimate.

Startup and bookkeeping are not progress. "Preparing the task directory" tells a watching human nothing about the work.

## Dev-Pipeline Workflow

`--workflow dev-pipeline` hands a task to the standalone `dev-pipeline` CLI, which drives an evidence-gated owner session in a target repository:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example \
  --workflow dev-pipeline --repo /path/to/target-repo
```

It supports `--runner codex` and `--runner claude`, because those are the owner runtimes the dev-pipeline core drives. `--operation start|resume|retry` chooses the lifecycle operation; a retry needs a new `--state-dir` and the previous one, so the earlier attempt stays immutable.

An installation supplies both `--assurance-config` and `--review-packet` to the
normal `start` entrypoint. The runner preserves the two paths across the detached
watcher boundary and the adapter forwards them to the same public
`dev-pipeline owner` process. The installation chooses the assurance strategy and
reviewer; task-agent does not invent either, but it does check that the reviewer
chosen is the family review admission bound to the launch. Dev-pipeline's
unassured path remains for a launch that review admission does not classify as
material; a material launch without assurance is refused before the author
starts, because nothing would ever ask its bound reviewer for a review.

For a supported `task_runner.py stop`, the runner first asks the public core to
bind `handoff request-stop` to the exact active review/rework lock. Only after
that durable write succeeds does it signal the process group. Ordinary resume
then reopens the same phase; an unknown disappearance remains fail-closed.

This workflow depends on the separate public `rdudov/dev-pipeline` project. The
tested commit is pinned in both requirements files, so the documented virtualenv
install provides the CLI. An editable local checkout can replace it during core
development. The runner resolves `.venv/bin/dev-pipeline` first and then `PATH`;
`TASK_AGENT_DEV_PIPELINE_BIN` or `--dev-pipeline-bin` selects an explicit
executable, and an unresolved CLI fails before launch.
Start, direct adapter invocation, and `review-candidate` all use this resolver.

`skills/task-runner/scripts/dev_pipeline_adapter.py` owns the integration. It writes `dev-pipeline/owner-instruction.md` from `task.md`, invokes the public `dev-pipeline owner` command, validates every neutral lifecycle event the core emits, and projects it into the task's own artifacts. Task-local state lives under `dev-pipeline/`: the core's lifecycle state in `core/`, the recorded event stream in `projected-events.jsonl`, and the two cursors that make recording and projecting separately restartable.

Ordering is enforced, not assumed. An event for another task, from a run the adapter is not following, or with a gap in its sequence is refused rather than absorbed; a repeat is ignored. A new attempt, or a new run inside the current attempt, is the one case that legitimately resets the cursor.

Projection is per-artifact:

- `status.json` gets the task state and current step, plus the attempt, run, and last event identity under `dev_pipeline`.
- `trace.md` gets one line per event, each carrying its event id as a marker so a replay cannot duplicate it.
- `progress.json` gets a concrete activity, and a `recent_outcome` only for events that report an actual outcome — startup bookkeeping never becomes one. The adapter never publishes `completed`/`total`/`unit`, because the lifecycle vocabulary carries no bounds and an invented total reads as a measurement. Anything the owner agent publishes itself wins: the adapter marks its own writes with `source: dev-pipeline-adapter` and leaves owner-authored progress untouched.

A clean subprocess exit is not a completion. Both profiles consume one durable
completion decision: task YAML frontmatter is `completed`, `plan.md` has no
unfinished markers, every required evidence gate's latest section records a
passing result, any required reviewer verdict is unambiguous, the independent
review the launch was admitted with has approved the work as it now stands, and
enforced policy families have an approved digest-bound review. A refused completion is
`blocked`; when the owner last published an incomplete bound, the record says
only that the run ended after that published lower bound and does not invent a
stopping point.

When that decision accepts a task that changed a Git repository, write admission
appends a `completion_accepted` receipt naming the write-scope run IDs covered by
that completion. A later task advancing the same repository does not
retroactively stale those accepted scopes; later rework under the original task
creates new scope IDs and therefore new obligations. Pre-receipt ledgers,
including a terminal abandoned scope, are backfilled only when task metadata,
runtime status, phase, and the matching controller attempt all durably say
`completed`. A review requirement added after an ungated terminal attempt also
requires that attempt's independent approval, and enforced policy families
are revalidated by the shared completion owner against the stored Git objects,
full context packet, native reviewer diagnostics, and decision envelope.
Terminal markers or a bare `approved` field alone never manufacture acceptance.
Historical acceptance records the validated candidate head and covers only scope
results at or before that head; a later same-task scope remains outstanding.
The historical check does not require reconstructing a new subject from the
successor worktree or its now-current dependency checkout.

The bounded reviewer decides only the two prose policy families against the
exact candidate. Required live evidence remains a separate completion gate: a
pre-terminal policy review must not require a future delivery/completion receipt
or turn that honest pending state into a policy-family refusal.

Create the bounded policy review over the final committed candidate with:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py review-candidate \
  tasks/001-example --repo /path/to/target-repo
```

## Task Phases

One user goal keeps one task number. Review is a phase of that task, and so is
the rework a review asks for — neither gets a number of its own, and neither
appears from outside as unrelated work.

The phase vocabulary is this project's: `planned`, `implementation`, `review`,
`rework`, `live_acceptance`, `blocked`, `completed`, `failed`. `dev-pipeline`
owns the neutral lifecycle events and validates them; `task_phases.py` decides
what they mean for a task. Both profiles produce the same vocabulary — a
`dev-pipeline` run from its events, a `standard` run from what the run was asked
to do and what the task has already been through — so an observer reads one
thing regardless of how the work was executed.

`phases.json` in the task directory is the durable record: the current phase and
an append-only history, each entry naming what caused the transition. Re-entering
the phase a task is already in appends nothing; entering a phase it held before
does, which is why `implementation → review → rework → review → completed` reads
as five entries under one number.

Two mappings are worth stating because the obvious reading of each is wrong:

- A checkpoint during rework stays `rework`. The core emits the same event
  before and after a review, and reading it as `implementation` both times would
  erase the rework phase at the moment it is happening.
- `review_waiting` and `review_refused` are `blocked`, not `rework`. The review
  could not be obtained or cannot be trusted, so the work stops for a human.
  Sending it back around the loop would let it close having never been reviewed.

An unknown event kind moves nothing. A newer core may emit a kind this project
has not heard of, and leaving the task in the phase it is demonstrably still in
beats inventing one.

## Review Admission

A material launch that nobody independent can check is refused before the author
starts. `review_admission.py` decides that at launch time, and the decision is
recorded in `.runner/review-admission.json`, `status.json`, and `trace.md`
whatever the outcome.

Two things are decided, both from observable inputs:

**Does this launch need a review?** From what the launcher itself can see: a
write grant on a target repository, a sandbox mode that is not `read-only`, a
non-standard workflow that delivers a candidate, a gated task contract, declared
review gates, a registered `deliverables/manifest.json`. Anything observed puts
the launch in `material`, and an undeclared launch is `material` too — silence is
not an exception.

**The narrow read-only exception** is a structured declaration in
`task_contract.json`, never an adjective in prose:

```json
{"review_policy": {"work_class": "read_only_lookup",
                   "justification": "one-off lookup, no state change"}}
```

The declaration only holds while nothing contradicts it. Declare
`read_only_lookup` and then grant write access, and the launch is material with
`classified_by: declared_read_only_lookup_contradicted_by_observation`. Calling
work trivial in `task.md` classifies nothing at all.

**Which assurance strategy is bound?** `--assurance-config` is the existing
public `dev-pipeline` installation contract and is read before reviewer
resolution under either workflow. `cross_provider` binds the configured other
provider; `isolated_same_provider` binds the configured author provider but only
through the review command's fresh read-only session; `live_acceptance_only`
binds its named live scenarios and records that no model verdict exists. The
public contract is provider-neutral, but Task Agent's admission policy still
refuses Cursor as a reviewer under every strategy.
An unavailable configured provider refuses without fallback.

With no assurance configuration, behavior is unchanged: Codex work is reviewed
by Claude and Claude work by Codex when that CLI is installed; Cursor remains an
author compatibility runtime and never a reviewer. `--reviewer-runner`,
or `review_policy.reviewer_runner`, can name Codex or Claude explicitly. A host
with no independent default family refuses before author start.

The admission record always names `assurance_strategy` and its source. This is
not cosmetic: consumers must be able to distinguish cross-provider review,
same-provider session isolation, and live acceptance without interpreting a
generic `approved` flag as a stronger guarantee.

The refusal exits non-zero with its reason in its own words, and leaves the task
`blocked` with the same message in `status.json` and `trace.md`. It is also
delivered to whoever asked for the launch, as a `pipeline_stopped` notification
carrying what happened, what to do about it, and the workflow that was refused —
an installation routing or auditing by workflow cannot read that out of the
message text. Task state is something a caller has to go and look at, and a gate
whose whole value is being heard before any author work cannot rely on that. The delivery outcome is recorded next to the
decision as `review_admission.notification`, and a transport that is absent or
broken leaves the refusal exactly as it is — nothing can turn it back into a
launch. It happens before the child is spawned, before the launch's application
policy is prepared, and before Git write admission — the point of it is that no
author work is spent.

**Only a launch that started an author binds anything.** The admission says which
family authored this number's work, so it is decided early and committed late:
`admit_launch` decides and refuses, and `commit_admission` — called where the
watcher is spawned, after the application launch policy, the workflow command and
the prompt — is the single act that makes this launch the number's author.
Refusals that arrive after it, from the watcher that could not be spawned or the
watcher that refused before its child, append an `annulled_admission` entry and
restore the previous record: the ledger is append-only, so a withdrawal is a fact
added rather than history edited, and `bound_author_admission` skips what was
withdrawn.

**The withdrawal outlives the process that committed.** The commitment is written
to `.runner/review-admission-commitment.json` with the launch token, not kept in
the launching process, because the failures that end such a launch are routinely
reached without it: the detached watcher is supervised independently and can
refuse before its child long after the parent is gone, and a parent killed
between committing and spawning leaves no process at all. So the parent's
`abort_start` and the watcher's `report_launch_failure` both withdraw — whichever
gets there, once, for its own launch token, and before the pending launch claim
is released, since releasing it is what makes the task startable again.
`confirm_admission`, called by the process that spawns the child, is what ends
the commitment; until then `bound_author_admission` does not read the binding it
made, so a launch nobody is left to withdraw still binds nothing. After it, the
binding is final: the author may already be writing, and a late refusal must not
hand that work back to the pair the number had before it.

`--dry-run` never reaches that commit, and is additionally evaluated and refused
exactly like a real start — that report is what preparing a launch is for —
without writing its refusal or allocating a number for a review outage it merely
predicted.

Every one of those paths used to leave a launch that wrote no line of work
recorded as the latest author, which is enough to lock the bound reviewer out of
its own number as "the author's own family" and admit the family that wrote the
work in its place. The sibling Git write admission has always worked this way: a
dry run opens no write scope, and an abandoned claim is resolved by measurement
rather than by trusting the claimant to still be alive.

**The binding is carried into the run, not just recorded.** A named reviewer
that nothing consults is a note in a file, so the same decision governs both
places where the work could still get out unreviewed:

- a `dev-pipeline` launch is reviewed by the core, using the assurance the
  installation supplies. The launcher checks that this is the same strategy and
  provider binding it just admitted. A model-review strategy naming another
  provider, or a material dev-pipeline launch carrying no assurance at all, is
  refused with `assurance_binding` on the decision saying why. Explicit
  `live_acceptance_only` legitimately names no reviewer and remains gated by its
  scenario evidence. There is no unassured path for material dev-pipeline work.
- a launch asked for a verdict (`--require-review-verdict`) *is* the review. It
  is admitted as `work_class: review`, and it has to be the provider this number
  was promised. The author's family is refused under `cross_provider`, but is
  exactly the required provider under explicit `isolated_same_provider`, and a
  same-provider launch is refused unless its observed sandbox mode is explicitly
  read-only and its grant allows no write. In both admitted
  cases this command creates the separate read-only session. A third provider is
  refused. `live_acceptance_only` refuses a model-review launch rather than
  depicting a verdict. A review whose subject is another task number has no
  binding here to contradict and is left to whoever owns that pairing.

**Acceptance is bound to it too.** `independent_review_status` answers, from this
number's own append-only ledgers, whether the work as it now stands carries the
assurance it was admitted with. Model-review strategies require the latest round
from the exact bound provider to approve, with no author phase entered since;
same-family approval counts only for explicit session isolation. Live-only
assurance requires no round, refuses if one is fabricated, and standard
completion checks every configured scenario against the append-only
`verification.md` results. The shared completion decision refuses otherwise and
names the exact missing condition. A material standard model-reviewed launch
therefore ends `blocked`, waiting for a review phase of the same number:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py review tasks/001-example
```

A standard review's decision reaches the ledger from the one canonical `Verdict:`
line its contract already requires. When a new admitted review starts, the
runner removes prior canonical verdict lines from `findings.md` while retaining
the surrounding findings; historical decisions already live in
`reviews/rounds.jsonl`. A launch that creates no reviewer restores the prior
line. After installation-specific pause handling, a clean reviewer exit records
the round before completion is evaluated. An approval that passes every other
gate closes task frontmatter through `tasks_index.py` and leaves `status.json`
completed. A
refused, failed, blocked, or paused run reconciles frontmatter to `blocked`, so
an observer that reads the canonical metadata cannot mistake unfinished work for
finished work.
Finding-level repeat detection needs the structured findings only a dev-pipeline
decision artifact carries.

There is no cap on rework rounds. Review and rework are phases of one task
number, and the runner has nothing that counts down: `rework_rounds` is recorded
as `unlimited` for exactly that reason. An unapproved round refuses this
acceptance and authorizes the next round; it never says "no more". A technical
limit inside one provider attempt may end a *process*; continuing the same goal
after it needs no user permission. For the same reason the pinned dev-pipeline
revision is one with no review-round limit of its own — a dependency that stops
at a count and asks the user whether to continue would reintroduce the budget
this project does not have.

What the round ledger at `reviews/rounds.jsonl` is for is quality, not budget.
Each reviewer decision the dev-pipeline adapter projects is appended with the
finding identities it carried. When a finding this task already demonstrated
comes back, the projection adds an execution-quality warning to `status.json`,
`progress.json`, and `trace.md` — and changes nothing else. Rework continues. A
finding appearing for the first time, including the deeper one a fix exposes, is
normal iteration and warns about nothing.

A defect in the review infrastructure itself gets a task number of its own, and
the runner files it rather than leaving the rule to prose. When a launch is
refused because no reviewer is installed, and when a `review_waiting` or
`review_refused` event says a review could not be obtained, a task is allocated
through `skills/task-creator` — which owns task identity, so the launcher asks
for a number instead of becoming a second allocator — and the entry appended to
`reviews/infrastructure-obligations.jsonl` names the filed task, the defect, and
the fact that this task's subject and work are unchanged. `status.json` and
`trace.md` say where the defect went.

Repeats of one outage reuse the number already filed for it, keyed on the
defect, so retrying a launch against a host that is still missing a reviewer does
not fill the index with copies of one problem. When there is no workspace
`tasks/` root to file into, the entry says the defect is `unfiled` and why —
never that it was filed.

The work under review is not rewritten to accommodate the outage, and the outage
never licenses a weaker assurance level: the task keeps its scope and waits for
the configured provider or evidence. Naming the author's family without an
explicit `isolated_same_provider` installation strategy remains an incoherent
launch and is simply refused.

## Git Write Admission

Two write-mode runs in one repository do not produce two reviewable candidates;
they produce one working tree nobody can attribute. Before a write-mode child is
spawned, `write_admission.py` takes a lock in the Git common directory, durably
settles provable abandoned no-ops, rechecks every blocker, and appends the
opening claim before releasing that same lock. It records what the run actually
did in an append-only ledger at `.runner/write-admission.jsonl`.

A launch is refused when:

- another task holds an open scope in the same repository and is live or cannot
  be proven absent (`live_overlapping_write`);
- another task changed the repository and its own completion gates have not
  closed (`unreviewed_overlapping_write`);
- this task has an older scope whose claimant may still be live or whose
  repository can no longer be measured (`unresolved_own_write_scope`).

A task's *own* unreviewed change never locks it out: repairing that change is
what the rework phase is, and it happens under the same number.

The ledger shape matters, because a single mutable result field had two failures
that independent review reproduced. A read-only or dry run wrote its own
"changed nothing" over an outstanding "changed something", and the obligation to
review that earlier change vanished. And a run that opened a scope and never
closed it became an indeterminate result that blocked every task in the
repository. Here a dry or read-only run opens no scope and appends no result of
its own, so it has nothing to overwrite; a dry run may only transfer an exact
previous run's terminal evidence before replacing current-run metadata. An
abandoned scope is durably closed as a no-op
only while the repository still matches its opening fingerprint. A foreign
claimant with unknown liveness refuses another writer without being settled as
dead. If that exact run's own `runner.json` records its matching
`write_scope_run_id` and a terminal outcome, the next dry run or real `start`
transfers that evidence into the append-only admission ledger before replacing
current-run metadata. The scope therefore remains settleable across PID namespaces for its
owner or a successor; a terminal record for any other run does not count. A
dead scope with a divergent fingerprint is a recomputed obligation for other
tasks, cleared by a revert or the owner's completed gates; the owner may
continue rework under its existing number while the old ambiguity remains
recomputed rather than durably attributed. An unmeasurable repository still
refuses the owner. If the
original writer later delivers its real close, that record supersedes an
observer's earlier synthetic settlement. Repository state binds
HEAD, tracked worktree bytes, staged bytes, and non-ignored untracked
path/type/content identity. Ignored runtime files are outside that publishable
state.

Host observers consume the same liveness owner through the public
`process_identity`, `process_is_live`, and `runner_pid_namespace_state` API.
The namespace state distinguishes local visibility, a visible foreign
namespace, a provably absent recorded namespace, and a foreign namespace whose
absence cannot be proved. Installation adapters may preserve that vocabulary
but must not reimplement its `/proc` and host-supervision decision.

A launch failure before the child exists is terminal launcher state. It removes
`launch_pending` when it records `failed_to_launch`, so a retry is not refused by
the dead launch's ownership token.

A scope measures the repository, not the child. It records the tracked state
before the run and after it, so anything that changed the repository in between
is attributed to the run — including a human editing the same tree. That is
sound under the discipline admission enforces, where only one writer holds a
repository at a time, and it is the reason the record says *the repository
changed* rather than *this child changed it*.

Whether an outstanding change has been reviewed is not decided here. This module
reports it and the completion owner decides. Pairing policy — who may review
whose work — belongs to the installation that defines it.

## Actuality

`task_engine.py actuality` reports how long ago a task was observably touched,
measured from the filesystem. A child's own `updated_at` is a claim: a child
that stalls can leave a fresh timestamp in a file it wrote before it stalled,
and one that dies cannot correct the last it wrote. The mtime of
`progress.json`, `status.json`, `trace.md`, `phases.json` and `verification.md`
is an observation. `.runner/` metadata is excluded deliberately, because the
supervisor touches it on its own schedule and would report freshness the work
does not have.

`TASK_AGENT_ACTUALITY_STALE_SECONDS` sets the reporting threshold; it defaults to
900. It is a reporting threshold, not a control — a run that publishes every few
minutes and one that compiles for half an hour are both healthy.

### Application Adapter API v1

The public engine is installable as `task-agent-engine` and exposes a versioned
installation seam in `task_agent.application_adapter`. Register API v1 with:

```bash
task-agent start /absolute/task --application my_app.task_agent:adapter \
  --destination "$INSTALLATION_DESTINATION" --memory-limit 4G
```

The v1 methods are `launch_policy`, `standard_session`,
`standard_run_finished`, `deliver_event`, `recover_transport`, and
`completion_problems`. They
supply installation values and policy only. The engine continues to own
process supervision, session-state persistence, event
validation/order, artifact projection, and completion refusal.
The projector follows the public core's explicit run boundaries within one
attempt: owner start/resume, independent review, same-session rework, live
acceptance, and an escalated phase blocker may each carry a fresh run identity.
An arbitrary event that changes run identity without one of those boundaries is
still refused.
On adapter restart, the authoritative core ledger is replayed through the same
validator before a new core command runs. Previously consumed event IDs are
no-ops; a missed event is projected and offered to transport once, while an
invalid sequence or identity still refuses continuation.

- `--destination` is opaque. Only its digest may be persisted; never put the
  raw recipient into runner metadata, task artifacts, source, or docs.
- `standard_session` can return native `--session-id`/`--resume` arguments and
  non-secret JSON state. This is the supported standard-workflow continuation
  seam after an application observes an exact quota reset. Secret-bearing state
  keys are refused. The parent forwards the application, operation, and opaque
  destination across the detached watcher boundary; the watcher binds them to
  the exact prepared record and fails the launch if registration, destination
  binding, or native-session data is missing or changed.
- `standard_run_finished` receives the supervised return code and log. An
  installation may parse a structured exact reset, arm its own durable
  scheduler, and return `waiting_for_quota`; it may not invent a reset time or
  replace the native session.
- `deliver_event` receives notable neutral lifecycle events after validation,
  including independent-review start, required rework, review refusal, and an
  exact quota wait.
  Recipient binding, delivery receipts, deduplication, replay, and Telegram are
  installation concerns.
- `recover_transport` receives the durable validated event-log path when the
  projector starts. An installation reconciles its own receipts there; the
  engine never guesses whether repeating a transport send is safe.
- `completion_problems` can enforce cross-family verdict binding and the
  delivery policy of one installation. Problems join the shared completion
  refusal; prose or a child-written `completed` state cannot override them.
- An installation whose terminal side effect itself establishes required live
  evidence may declare those exact ids in
  `completion_preparation_evidence_ids` and implement `prepare_completion`.
  The declaration is an application capability list; each task defers only the
  intersection with evidence ids its effective contract actually enforces.
  The request carries that exact intersection, and the application performs
  only those selected terminal gates.
  The engine calls this optional v1 hook only when the predicate passes with
  exactly those ids and terminal task status deferred. The hook must persist its
  evidence before returning. On success the engine closes metadata through the
  canonical `task-agent-tasks-index set-status` command, then immediately
  evaluates the complete predicate; failure or partial work leaves metadata
  non-complete and remains a refused completion. Applications without the hook
  retain the old order and their owner closes metadata as before. The command
  uses the repository-owned
  `tasks_index.py`; an installed runtime resolves the console entrypoint beside
  its active Python interpreter, including when an adapter loaded the engine as
  top-level modules. When the hook's deferred precondition or preparation itself
  refuses, projection carries
  that exact reason and marks the refusal `automatic_finalization`; it must not
  recompute a full predicate whose still-deferred status check masks the
  blocker. Installation messages use that marker to keep recovery with the
  owner and must not ask the user to run the finalizer-owned metadata transition.
  An exception after preparation, including canonical metadata-owner refusal,
  becomes the same durable automatic-finalization refusal: projection advances
  and the installation receives the rejected terminal event so it can correct
  any completion statement already sent during preparation.

A validated dev-pipeline quota-wait event remains a durable waiting state after
the adapter process exits. Every child-written `completed` state is rechecked
through the full engine predicate plus the registered application policy before
the runner accepts it. The delivered-candidate policy-family review is specific
to dev-pipeline; standard tasks use their authored evidence and verdict gates.
The watcher records `succeeded` only after that acceptance; a clean exit refused
by the gate records `rejected_completion_contract`, and a quota pause records
`waiting_for_quota`.

With no registered application the default v1 implementation is inert, adds no
completion policy, and refuses standard `resume|retry` because their native
meaning would otherwise be invented.
Transport recovery is bound to the adapter cursor's active attempt. The public
engine still exposes the complete append-only projected ledger for audit, but an
installation must not reinterpret terminal events from older attempts using the
current task status or current refusal text.

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
- When `task_contract.json` is present, treat it as the execution contract for delegated work, review, and completion gates.
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
