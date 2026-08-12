# Task Agent Project Rules

This repository contains a generic assistant workspace built around autonomous agent workflows.

## Core Rule

Any substantial change to the agent engine must update documentation for both agents and humans in the same change.

A substantial change includes:

- task lifecycle or task artifact structure
- skill discovery or skill execution rules
- agent orchestration behavior
- restore, resume, or publication conventions

If a change modifies one of these areas, update the relevant project docs before finishing the task.

## Documentation Boundaries

- `AGENTS.md` defines project-wide operating rules.
- `docs/` explains repository architecture and workflows.
- Each skill documents its own behavior in its own `SKILL.md`.

Do not duplicate skill-specific instructions in project-level docs.

Agent entry points must not duplicate rules either. `AGENTS.md` is canonical for
project rules, `.cursor/rules/*.mdc` for always-on rules, and `skills/` for
skills. `CLAUDE.md` only imports them and `.claude/` only holds symlinks into
them. Edit the canonical file, never a `.claude/` path, and add a `CLAUDE.md`
import line when a new Cursor rule is added. See
[docs/claude-code-setup.md](docs/claude-code-setup.md).

## Task Conventions

Every substantial user task gets its own directory under `tasks/` before substantive work begins. The default is to create a task; skipping is only appropriate when the request is clearly trivial or the user explicitly opts out of task artifacts.

A request is substantial when any of these apply:

- it has more than one meaningful step, such as research, implementation, verification, or documentation
- it creates or changes scripts, configuration, services, documentation, or durable artifacts
- it needs live checks against external systems, APIs, browsers, SSH, or other runtime services
- it performs work in another repository or absolute path outside this workspace
- it is a follow-up that continues the same non-trivial goal

Before broad codebase search or live checks, use existing durable context first: `tasks/INDEX.md`, related task artifacts, and the local project/path index described by `data/local-projects.example.md`. Promote repeated lookup knowledge to an index, skill, or rule before closing the task.

`tasks/INDEX.md` is the canonical ordered task index for a local workspace. It is local generated state and is not tracked by the template; use `tasks/INDEX.example.md` as the format template. Task YAML frontmatter is the source of truth for task metadata, and `skills/task-creator/scripts/tasks_index.py` is the only interface that writes it.
- Each task directory must contain `task.md` and `plan.md`.
- Delegated or review-sensitive tasks may also include `task_contract.json` for structured non-negotiable constraints, forbidden substitutions, required live evidence, and completion policy.
- `task.md` should preserve original inputs that matter for execution, such as constraints, assumptions, acceptance criteria, and explicitly requested options.
- Keep tasks flat in `tasks/`; express hierarchy through `Parent Task` and `Related Tasks` in `task.md`.
- One user goal keeps one task number. Review and the rework a review asks for are phases of that same task, recorded in its own `phases.json`; do not create a separate task for either. The phase vocabulary and its event mappings are owned by `skills/task-runner/SKILL.md`.
- Only one task may hold a Git repository in write mode at a time. A write-mode launch is admitted against the target repository before the child is spawned, and what a run did to that repository is recorded in an append-only ledger under `.runner/`.
- Task-specific findings and sources belong in the task directory.
- A long-running child should publish substantive live progress in `progress.json`: a `schema_version: 1` object with a concrete `activity`, `updated_at`, and optionally `recent_outcome`. `completed`, `total`, and `unit` are published only together and only when the owner actually knows the bounds. Neither owners nor readers may infer a missing total, and startup bookkeeping is not an outcome.
- A task root may contain `USER_PREFERENCES.md` beside `INDEX.md`, using `tasks/USER_PREFERENCES.example.md` as the format. Agents read it before choosing an unspecified output representation and update it only from explicit, reusable user instructions, citing the task the instruction came from. The current request and later continuations override it. Do not turn one-off task requirements into defaults, and do not infer preferences from prose.

## Requested Deliverables

Files the user explicitly requested are a separate, user-facing class of output.

- Put each complete requested file in the task's `deliverables/` directory and list its basename in stable order in `deliverables/manifest.json` under a `deliverables` array.
- `findings.md`, `verification.md`, `sources.md`, and `trace.md` stay internal diagnostics. None of them substitutes for a different requested file, and they belong in the manifest only when the user asked for that record itself.
- Registration in the manifest, not a filename extension, decides what is delivered. Only contained regular non-symlink files with content are eligible; a malformed or unsafe registration is a blocker rather than a partial delivery.
- Validate a task's registered deliverables with `skills/repo-health/scripts/check_deliverables.py`. Enforcement that depends on an actual transport — content identity across restarts, delivery deduplication, per-message byte limits — belongs to the delivery-enabled runtime that owns that transport, not to this template.
- Before reporting completion, re-read the original request and every continuation and confirm the response and registered deliverables satisfy the latest complete intent. A later clarification may supersede an earlier requested representation. Do not infer requested formats from keywords.

## Durable Data

- Reusable data that should survive across tasks belongs under `data/`.
- Durable repository/path lookup indexes should live under `data/`; `data/local-projects.example.md` provides a generic starting format.
- Multi-task project records belong under `data/projects/`.
- Active durable projects should maintain a rolling status snapshot, typically `status.md`, when that context matters across tasks.
- Local task and data artifacts are not a substitute for committing and pushing source changes.

## Skills

- Skills live under `skills/`.
- Use `skills/task-creator/` to create task artifacts.
- Use `skills/task-runner/` to delegate substantial work to child agents or run a task through the dev-pipeline workflow. The child runner follows the parent CLI agent unless a caller selects one explicitly, so a Codex parent delegates to a Codex child and a Claude parent to a Claude child. That resolution belongs to the task runner, not to prompt text, so callers that never read a skill get the same behavior. Every run records the resolved runner and the rule that resolved it. A launched run is supervised by kernel process identity so a recycled pid is never mistaken for the child.
- `--repo` is an access input for both workflows. Standard runs grant the
  resolved target through the selected runner and verify it before a write-mode
  launch; dev-pipeline passes it to the core owner. New supervision records also
  bind liveness to the observer's PID namespace, so a nested observer cannot
  declare an invisible host process dead or replace it.
- Review admission decides, before the author starts, whether a launch needs an
  independent reviewer and whether one can be bound. Material work is recognized
  from observable launch effects, not from prose calling it small, and the only
  exception is a structurally declared read-only lookup that observation does not
  contradict. An author never reviews its own work, so a material launch with no
  independent provider family available is refused up front instead of after a
  spent attempt. There is no limit on rework rounds: review and rework stay
  phases of one task number until the work is accepted, a repeated demonstrated
  finding is reported to the user as an execution-quality problem without
  stopping the fixes, and a defect in the review infrastructure is filed under
  its own number through the task-number owner rather than becoming the subject
  of the task that hit it or a reason to accept unreviewed work.
- Git write admission is one common-directory-locked check-and-claim operation;
  only provable abandoned no-ops are durably settled before a successor enters,
  unknown liveness refuses a foreign writer, abandoned divergent work remains a
  recomputed obligation for other tasks, and the owner can enter same-number
  rework without freezing ambiguous attribution. Before a dry run or real start
  replaces the single-current-run metadata, it transfers a matching prior
  terminal record into the append-only admission ledger; that exact run-scoped
  evidence recovers its scope across PID namespaces. A terminal launch failure releases
  its pending launch claim. The fingerprint includes staged and non-ignored
  untracked content. Successful completion appends the exact write-scope run IDs
  whose gates closed, so later repository history cannot retroactively invalidate
  an accepted task while later same-task rework remains a new obligation.
- Use `skills/task-runner/scripts/task_engine.py` to ask what a task is and where it stands: `state`, `phases`, `actuality`, `admission`. It is the public surface for anything downstream — a product layer, a transport adapter, another installation — and it composes the modules that already own each decision. Do not import internals out of `task_runner.py` to answer a question this surface answers.
- Use `skills/task-artifacts/` during task execution to update `verification.md`, `findings.md`, and related files at checkpoints (not only in chat).
- Use `skills/project-organizer/` for multi-task durable project records.
- Use `skills/skill-maintainer/` when adding, restoring, or changing skills.
- Use `skills/repo-health/` after restores, before publishing a sanitized template, or when task/data/skill artifacts may be inconsistent. It also owns `check_pre_push.py` for outgoing-change leak checks and `check_deliverables.py` for the registered-deliverables contract.

## Execution Contract

When a parent agent delegates a task to a child agent, the task directory is the source of truth.

Before delegation, the parent agent should ensure `task.md` and `plan.md` contain enough context for independent execution. If the task has non-negotiable constraints, forbidden substitutions, or mandatory live verification gates, record them in `task_contract.json`.

Both standard and dev-pipeline runs use the same durable completion decision.
Exit code zero is refused unless task frontmatter is `completed`, the plan has no
unfinished markers, required evidence has a latest passing result, and required
review gates are established. A cross-review task may require its author to
publish exactly one canonical `Verdict:` line in `findings.md`.

Substantial implementation work should preserve at least one no-mock end-to-end verification path for the primary function being changed. Services, APIs, daemons, and workers should include a smoke check against the real runtime entrypoint in the target launch mode.

When changed source is loaded by an active local service, daemon, worker, or unit, completion includes applying the change to those running units: restart or reload them, confirm they are running with fresh start timestamps, inspect recent logs for startup errors, and record that evidence. If restart is intentionally deferred or blocked, the final response and task artifacts must say so.

When a deliverable's correctness materially depends on visual rendering, structural validation is only a preliminary check. Render or open it in a real renderer, viewer, or browser and inspect the resulting images. Inspect every slide of a short presentation; for a longer document inspect the first, last, and representative intermediate pages, widening coverage on layout risk or after finding a defect. Check clipping, overflow, overlap, missing or broken images, font substitution, unreadable sizing or contrast, and broken responsive or print layout where relevant. HTML must be exercised in a real browser at representative viewports. Package integrity, DOM parsing, text extraction, and static assertions alone are insufficient. This rule is format-extensible: use available renderers and install task-local tools for a new format rather than maintaining a closed list. Keep render evidence internal by default. An unavailable mandatory render or an unresolved visual defect blocks completion unless the task contract explicitly permits a recorded gap.

Verify artifact completeness against the best available source of truth before reporting success. For text, code, and documents, compare size, line count, and start/end boundaries against the original input or source log, and look for truncation markers. A successful write or transport operation proves delivery, not completeness.

Stub-first implementation is acceptable only for newly introduced seams or genuinely unavailable external dependencies. Do not replace an existing, exercisable production integration, provider path, API client, or runtime entrypoint with a stub when the task requires that path. A stub-only task may scaffold behavior or tests, but it does not earn credit for live evidence or required task-contract gates.

Do not assume backward compatibility, legacy fallback behavior, or a compatibility migration unless the user request or task contract explicitly requires it.

Agents must preserve the semantic target of the request instead of substituting a nearby implementation that merely produces a similar surface effect. When the user names a reference behavior, artifact, provider, model, protocol feature, or runtime branch, implementation and verification must exercise that named path directly, or the task must record the deviation as a blocker or explicit scope change.

When an agent-caused mistake materially affects a task, the correction is not complete until the agent performs a short mistake review and updates the relevant rules, docs, skills, scripts, or task contract so the same failure is less likely to repeat. If the prevention change is broad, risky, or requires a significant redesign, record the analysis and present concrete options to the user before implementing it.

That review should be systemic rather than a pile of narrow examples. Before changing code in a corrective task, identify the semantic target the user expected, the intended production path that should have satisfied it, the layer that owns that path, the evidence showing where it diverged, and the verification that will prove the real path now works. Do not add a second mechanism at a nearby layer when an existing layer already owns the decision; fix the owning layer unless the task explicitly changes the architecture.

If behavior diverges by threshold, mode, provider, credential, feature flag, model path, transport, or fallback branch, each production-relevant branch touched by the task needs explicit validation evidence or an explicitly recorded verification gap.

If a task contract marks a live/no-mock verification path as required, environment-blocked execution of that path is a blocker for approval or completion, not a non-critical note.

Tests around helpers, fixtures, mocks, fake models, or test-only harnesses do not count as production validation for a branch that can realistically run in production.

When a task modifies git-tracked source in a repository with a configured remote, completion should include committing and pushing the finished change unless the user explicitly asks not to, the work is intentionally left uncommitted for review, or credentials/network/policy block the push. Before creating a branch or pushing, fetch the remote and make sure the base branch is current. Before pushing, run the repository pre-push leak check and review the outgoing diff. If push is deferred or blocked, the final response and task artifacts must say so explicitly.

If a local, unpushed commit is discovered to be wrong and there are no clear contraindications such as dependent work, shared review state, or user-owned changes on top, prefer rewriting local history with `git reset` or another explicit history repair over adding a revert commit that preserves noise. Before resetting, inspect the affected repository status and commit graph; after resetting, record the old and new HEADs in the task trace. Use a revert commit when the bad commit was already pushed/shared or when preserving an auditable public history is explicitly required.

After backup restore or before publishing a sanitized copy, run the repository health check and record any gaps in the active task artifacts before relying on local task, data, or skill state.

## Remote Safety

For remotely triggered work, prefer the least destructive interpretation that still satisfies the request.

- Normal engineering edits, refactors, and deletions of specifically requested files are allowed.
- Cross-project work is allowed when it is explicitly part of the request and limited to the relevant project.
- Clearly destructive broad actions are not allowed by default.
