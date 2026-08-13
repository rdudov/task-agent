# Task Agent

Task Agent is a small, forkable workspace for task-first autonomous-agent workflows.

It is intentionally generic: no private task history, no local data, and no bundled personal integrations. Project-level operating rules live in [AGENTS.md](./AGENTS.md).

## History Rewritten On 2026-08-06

Every commit before this date was rewritten to remove deployment-specific host
paths and private project, task, and trip names that the earlier commits still
carried in examples. Only those strings changed: the tree at the tip is
byte-identical to what it was before the rewrite. Commit hashes did change, so a
clone made before 2026-08-06 has no commit in common with `origin/main`.

If you have such a clone, discard its local history:

```bash
git fetch --all && git reset --hard origin/main
```

Commit anything you want to keep to a separate branch first; the reset discards
uncommitted and unpushed work. One merged leftover branch was renamed in the same
change and is now `port-generic-agent-workspace-work`; prune stale
remote-tracking refs with `git fetch --prune`.

## What Is Included

- `tasks/` skeleton for durable task artifacts
- `tasks/USER_PREFERENCES.example.md` as a starting point for durable user defaults
- `data/projects/` skeleton for multi-task project records
- `data/local-projects.example.md` as a starting point for local repository/path indexes
- `AGENTS.md`, `.cursor/rules/`, and `CLAUDE.md` as one shared rule set for Codex, Cursor, and Claude Code
- `skills/task-creator/` for creating task directories and updating the index
- `skills/task-runner/` for parent-child CLI agent execution, detached-run supervision, and the dev-pipeline workflow
- `skills/task-artifacts/` for keeping task artifacts current during work
- `skills/project-organizer/` for durable project records
- `skills/repo-health/` for restore, publication, deliverables, and pre-push checks
- `skills/skill-maintainer/` for creating or changing skills
- `docs/` for architecture, task execution, Claude Code setup, and self-development workflows

## Quick Start

Prerequisites are Python 3.11+, Git, network access, and an installed and
authenticated Codex, Claude, or Cursor CLI for the `dev-pipeline` owner. Clone the
repository, then from its directory create the environment and install the
pinned public dependency together with the test tools:

```bash
git clone https://github.com/rdudov/task-agent.git
cd task-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

The repository is also a Python distribution. An application can bind the
engine to an immutable Git revision without copying its lifecycle code:

```text
task-agent-engine @ git+https://github.com/rdudov/task-agent.git@<40-character-commit>
```

That install exposes `task-agent`, `task-agent-engine`, and
`task-agent-tasks-index`. Installed completion finds that metadata entrypoint
beside its active Python interpreter even when an application adapter loads the
engine as top-level modules. A post-preparation metadata-owner failure is
projected as a durable refusal instead of aborting projection, allowing an
installation to correct any terminal statement already sent. `TASK_AGENT_ROOT`
selects the installation workspace
for relative task paths; absolute task paths need no workspace convention.
Consumer runtimes and services should use this exact reviewed revision and a
lock file. Editable installs are reserved for isolated contributor virtual
environments where source edits are intentionally live; they must not back a
shared runtime or service.

Create a task:

```bash
skills/task-creator/scripts/create_task.sh "Example task" "Try the task-agent workflow"
```

Run health checks:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py --allow-empty-tasks
PYTHONPATH=skills/task-runner/scripts .venv/bin/python -m pytest skills/task-runner/tests skills/task-creator/tests skills/repo-health/tests
```

Before pushing a source change from this workspace, run:

```bash
.venv/bin/python skills/repo-health/scripts/check_pre_push.py --remote origin
```

To block deployment-specific project/task/trip names without publishing them,
put one literal per line in ignored `.state/private-history-markers`, or point
`TASK_AGENT_PRIVATE_HISTORY_MARKERS` at another local file. The guard also
refuses foreign remote and unknown ref namespaces while allowing ordinary local
branches, tags, notes, and stash.

An empty marker list is not a pass. A fresh clone has no `.state/`, so the name
check has nothing to compare against; both `check_pre_push.py` and
`check_repo_health.py` now say so on stderr instead of reporting a clean run.
Pass `--require-private-history-markers` to `check_pre_push.py` to turn that
notice into a failure.

## Agent Entry Points

The same rules reach Codex, Cursor, and Claude Code without being copied. `AGENTS.md` holds the project rules, `.cursor/rules/*.mdc` hold the always-on rules, and `CLAUDE.md` imports both rather than restating them; `.claude/` contains only symlinks into the canonical files. Adding a Cursor rule means adding its `.claude/imports/` symlink and its `CLAUDE.md` import line — see [docs/claude-code-setup.md](./docs/claude-code-setup.md).

## Delegating To A Child Agent

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example
```

The child runner follows the parent CLI agent, so a Codex session delegates to Codex and a Claude session to Claude. Pass `--runner codex|claude|agent` to decide explicitly, or set `TASK_AGENT_CHILD_RUNNER`. All three drive both workflows: under `dev-pipeline` the `agent` runner becomes the core's `cursor` owner runtime. Every run records which rule decided.

Access level is expressed once through `--sandbox-mode` (`read-only`, `workspace-write`, `danger-full-access`) and mapped per runner. `TASK_AGENT_WORKSPACE_ROOT` sets how far full access reaches; it defaults to the parent of this checkout.

For the standard workflow, `--repo /path/to/target-repo` makes that repository
an additional workspace/access root for Codex, Claude, or Cursor Agent while
the task-agent checkout remains the primary workspace.
Write modes verify writability before launch and record the result.

Material work binds an independent reviewer before its author starts. When the
author finishes, run that reviewer as the next phase of the same task; the
launcher selects the already-bound family and gives it read-only access:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py review tasks/001-example \
  --repo /path/to/target-repo
```

Three records remain because they answer different acceptance questions:
admissions preserve who promised to review, rounds preserve what that reviewer
decided, and phase history detects author work after approval. Installations add
transport and resource policy; they do not keep another pairing record.

Only one task may hold a repository in write mode at a time. A write-mode launch
uses one Git-repository-locked check-and-claim operation, and is refused while
another task is writing there or has changed it without closing its own gates.
The claim binds staged and non-ignored untracked content as well as tracked
worktree state. Unknown liveness refuses a foreign writer rather than granting
permission. A dead abandoned scope is durably settled when the unchanged
fingerprint proves a no-op. Before either a dry run or a real `start` replaces
the previous `runner.json`,
it transfers that run's matching terminal write-scope evidence into the
append-only admission ledger. That exact evidence can recover the scope across
PID namespaces after runner metadata replacement; another run's terminal record
cannot. A launch that ends before a child exists releases `launch_pending`, so
the terminal failure does not itself prevent a retry. Divergent work remains a
recomputed obligation for other tasks until it is
reverted or the owner's gates pass, while the owner may enter same-number rework
without freezing the ambiguous attribution.

`start` returns once the run is confirmed; the watcher and the child keep running in their own sessions, so closing the terminal does not end the work. On a host systemd machine the watcher gets its own transient scope; elsewhere the recorded boundary says it inherits the caller cgroup. Both processes are recorded by kernel start-time identity and PID namespace rather than by pid alone. An observer in another namespace reports liveness as unknown and cannot replace, stop, or reattach the host run. `reattach` restores a lost watcher and refuses when the pid was recycled or a watcher is already live.

`stop` records a public-pipeline `handoff request-stop` marker before signalling
a live dev-pipeline process. This lets an ordinary resume reopen the exact
review or rework phase and retain the author session. If the marker cannot be
recorded, `stop` refuses before sending the signal; unexplained process loss
therefore keeps the core's fail-closed orphan handling.

## Asking What A Task Is Doing

```bash
.venv/bin/python skills/task-runner/scripts/task_engine.py state tasks/001-example
```

One JSON document: task identity and status, the phase the task is in and the
sequence of phases it went through, contract gate status, whether completion may
be accepted and why not, observed freshness, and what is running. `phases`,
`actuality` and `admission --repo R` are the narrower views.

This is the public surface. A product layer, a transport adapter or another
installation asks here instead of importing internals, so nothing downstream
breaks when a helper is renamed.

**One goal, one number.** A user goal keeps a single task directory for its whole
life. Review is a phase of that task, and so is the rework a review asks for —
`implementation → review → rework → review → completed` is the history of one
task, not five, and `phases.json` records it with the cause of each transition.
Both execution profiles produce the same vocabulary.

**Actuality is observed.** Freshness comes from the modification times of the
task's artifacts, never from a timestamp a child wrote about itself: a child that
stalls can leave a fresh timestamp behind, and one that dies cannot correct the
last it wrote.

## Dev-Pipeline Workflow

`--workflow dev-pipeline` runs a task through the standalone `dev-pipeline` CLI, which drives an evidence-gated Codex, Claude, or Cursor owner session:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py start tasks/001-example \
  --workflow dev-pipeline --repo /path/to/target-repo
```

The workflow dependency is the separate public repository
[`rdudov/dev-pipeline`](https://github.com/rdudov/dev-pipeline). It is pinned in
`requirements.txt` and `requirements.lock`, so the Quick Start installs the
tested revision. To develop both repositories locally instead, replace it only
in an isolated contributor virtual environment with an editable checkout:

```bash
.venv/bin/pip install -e /path/to/dev-pipeline
```

Both standard-child and dev-pipeline-owner instructions apply the same
no-code-first order: do nothing, remove/disable, configure/reuse, simplify,
then add the smallest necessary code and justify why it is needed. Independent
review applies the corresponding avoidable-complexity criterion.

The pinned revision includes the core's provider-neutral assurance contract and
review events, and imposes no limit on review rounds: rework and review repeat
under one task number until the work is accepted, so a pin must never be moved
back to a revision that stops at a count and asks whether to continue. A configured review uses `review_started` and
`review_rework_required` for visible review/rework phase transitions;
`review_approved` keeps the task in review until the following lifecycle event
advances it. Older installed cores still degrade compatibly by omitting phases
for events they do not emit.

The engine is transport-neutral, but its extension point is now explicit and
versioned. `--application package.module:object` loads application API v1;
`--destination` passes an opaque installation-owned value that is hashed in
runner metadata and never stored in clear text. Notable neutral lifecycle
events—including independent-review start, required rework, review refusal,
and an exact quota wait—are offered to `deliver_event`. The default application is inert, so a
plain template still sends nothing. On restart, `recover_transport` gives the
application the durable validated event log so its own receipt policy can
reconcile delivery without the engine guessing whether a resend is safe.
API v1 also has an additive, optional pre-finalization capability: an
application may declare the exact live-evidence ids its `prepare_completion`
method can establish. The request carries the exact intersection of that
capability list with the effective contract, so the application performs only
the terminal work this task enforces. The engine invokes it only when every other completion
condition except authoritative task status already passes. After successful
preparation, the engine closes task metadata through the installed
`task-agent-tasks-index set-status` owner and evaluates the full predicate
again. A failed preparation never changes task status. Existing v1 applications
without the declaration keep the original ordering and still close metadata in
their owner workflow.

When that deferred predicate or the application preparation refuses, the
adapter preserves that exact reason in `status.json` and marks the refusal as an
automatic-finalization branch. It does not replace the preparation blocker with
the earlier full-predicate status check. Installation transports can therefore
explain the actual blocker without directing a user to perform metadata closure
that the registered finalizer owns.

The same adapter can return a launch memory policy for `--memory-limit`, attach
native-session arguments to a standard `start|resume|retry`, classify the
supervised exit as an exact quota wait for its scheduler, and add
installation-specific completion problems such as an unresolved document
receipt. The public runner still owns the process,
session-state persistence, event ordering, artifact projection, and completion
refusal. A child-written terminal state is rechecked through the same durable
engine gate before acceptance. An application owns only its resource values,
transport receipts, and scheduler. API v1 is importable as
`task_agent.application_adapter`; session state refuses secret-bearing keys.
For standard runs the parent forwards the registration, operation, and opaque
destination to the detached watcher, which reuses the exact prepared session
record. A missing or changed value is a visible launch refusal, never an inert
application fallback or a fresh native session. Because the raw destination is
not persisted, restart-time transport recovery must resolve its recipient from
installation-owned state.

By default the runner resolves the CLI installed at `.venv/bin/dev-pipeline`,
then falls back to `PATH`. `TASK_AGENT_DEV_PIPELINE_BIN` or
`--dev-pipeline-bin` can select another executable explicitly; an unresolved
CLI fails before an owner process is started.
The same resolver is used by normal runs, direct adapter invocation, and
`review-candidate`.

The owner closes task frontmatter through `tasks_index.py`, completes every plan
step, and records passing live evidence. A completion reported without those
durable gates is blocked. For a contract with mandatory prose policy families,
run the bounded reviewer over the final committed candidate:

```bash
.venv/bin/python skills/task-runner/scripts/task_runner.py review-candidate \
  tasks/001-example --repo /path/to/target-repo
```

## Documentation

- [docs/architecture.md](./docs/architecture.md)
- [docs/task-execution.md](./docs/task-execution.md)
- [docs/self-development.md](./docs/self-development.md)

## License

Task Agent is released under the [MIT License](./LICENSE).

## Local State

`tasks/` and `data/` are durable local artifacts. This template tracks only skeleton and example files; real task history, `tasks/INDEX.md`, and reusable data should be backed up by your own local backup flow.
