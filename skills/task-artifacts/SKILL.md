---
name: task-artifacts
description: >-
  Keeps task directory artifacts up to date during work: verification.md,
  findings.md, sources.md, plan.md status, trace.md. Use when executing or
  delegating any non-trivial task under tasks/, after smoke tests, research,
  or when the user asks to record results in the task.
---

# Task Artifacts (durable progress)

The task directory is the **source of truth**, not the chat. Update artifacts **during** work, not only at the end.

## Required files by phase

| Phase | Files | Action |
|-------|-------|--------|
| Start | `task.md`, `plan.md` | Status `in_progress`; contract in `task_contract.json` if gates exist |
| Research | `sources.md`, `findings.md` | Append sources; short findings per topic |
| Implementation | `plan.md` | Check off completed steps; note blockers inline |
| Verification | `verification.md` | **Every** live smoke / contract gate — redacted, no secrets |
| Follow-up | `findings-*.md` | Optional topic files (e.g. `findings-api.md`) |
| Delivery | `deliverables/`, `deliverables/manifest.json` | Every explicitly requested output file, registered by basename |
| Done | `task.md` | Status `done`; acceptance criteria `[x]` |

Runner-managed (when using task-runner): `trace.md`, `status.json`, `progress.json` — keep in sync with the table above.

## verification.md (mandatory for live checks)

Create on first smoke. Structure:

```markdown
# Verification: <task-id>

Date: YYYY-MM-DD
Environment: <repo>, IFT/prod, secrets redacted

## <gate_id or short name>

- Command: `<exact command without secrets>`
- Result: **OK** | **FAIL** | **GAP** (explain)
- Evidence: one-line outcome (counts, tool names, paths — no tokens)
```

If `task_contract.json` defines `required_live_evidence`, each `id` must have a matching `##` section before marking the task done.

**Never** paste API keys, tokens, or `.env` values.

## findings.md vs chat

After research, write:

- `sources.md` — links, file paths, or source identifiers
- `findings.md` — decisions, API shapes, and verification outcomes

Do not rely on the user re-reading chat for task-specific contract nuances.

`findings.md`, `verification.md`, `sources.md`, and `trace.md` are internal task records. They never substitute for a file the user explicitly requested.

## Requested Deliverables

Put every complete requested output file in `deliverables/` and list its basename, in stable order, in `deliverables/manifest.json`:

```json
{"deliverables": ["quarterly-report.docx", "summary.pdf"]}
```

Register an internal record only when the user asked for that record itself. Verify before marking the task done:

```bash
.venv/bin/python skills/repo-health/scripts/check_deliverables.py tasks/001-example
```

The checker enforces what a local template can: explicit registration, contained regular non-symlink files, non-empty content, no duplicates, and count/byte limits. It cannot know which files the user asked for — re-read the request and every continuation yourself. A later clarification may replace an earlier requested representation.

## Rendered Visual Evidence

When a deliverable's correctness materially depends on its rendered appearance, structural validation is only a preliminary check. Render or open it with a real renderer, viewer, or browser, capture the images, inspect them, and record the inspected coverage and evidence paths in `verification.md`.

- Inspect every slide in a short presentation. For longer documents, inspect the first, last, and representative intermediate pages, widening coverage for layout risk or after finding a defect.
- Check clipping, overflow, overlap, missing or broken images, font substitution, unreadable sizing or contrast, and broken responsive or print layout where relevant.
- Exercise HTML in a real browser at representative viewports. DOM parsing or static assertions alone do not count.
- Render page, slide, and comparable formats to images. Archive validity, text extraction, or shape-level checks alone do not count.
- Keep screenshots and renders as internal evidence; do not register them in `deliverables/manifest.json` unless the user asked for them.
- Do not assume a closed format list. Use available renderers, and install a task-local tool when a new format needs one.
- Record an unavailable required render or an unresolved visual defect as **FAIL** or **GAP**. Do not mark completion when the render is mandatory and gaps are not allowed.

## Context Discovery

Before broad search or live checks, prefer existing durable context:

1. `tasks/INDEX.md` and related `task.md`, `findings.md`, `verification.md`, and `sources.md`
2. `tasks/USER_PREFERENCES.md` when choosing an output representation the request left unspecified
3. local lookup indexes under `data/`, such as files based on `data/local-projects.example.md`
4. the target repository's own operating context before reviewing or changing it: root `AGENTS.md`, hidden agent instructions, tool configuration, CI files, declared runtime versions
5. repository documentation and source search

Record new stable paths or recurring lookup details in the task artifacts while working.

## Checkpoint triggers (do not skip)

1. Finished a plan step with external dependency → update `plan.md` + one line in `trace.md` or `verification.md`
2. Ran any smoke script → append `verification.md` immediately
3. Discovered scope change → update `task.md` Open Questions / Acceptance Criteria
4. Parent delegated to child → child must return with updated artifacts; parent verifies files exist before closing
5. Produced a requested output file → place it in `deliverables/`, register it, and run the deliverables check
6. Produced a rendering-dependent artifact → render it, inspect the images, and record the coverage in `verification.md`
5. Discovered reusable lookup knowledge → promote it to an index, skill, or rule before completion

## Helper script

Append a verification section without hand-editing headers:

```bash
skills/task-artifacts/scripts/record_verification.sh tasks/002-example \
  "agent_smoke" \
  "Full CLI run OK; trace: Grep turn 1; no secrets in log."
```

## Promotion

Before marking a task done, ask whether a future agent would otherwise rediscover the same fact from scratch.

| Knowledge type | Durable home |
| --- | --- |
| Local repository path, important artifact, recurring lookup fact | `data/` index |
| Repeatable workflow or CLI recipe | `skills/<name>/SKILL.md` or a skill reference file |
| Always-on agent behavior | project rule, such as `.cursor/rules/*.mdc` |
| One-off result tied only to this task | `findings.md` in the task directory |

If promotion is intentionally skipped, note why in `findings.md`.

## Completion checklist

Before `task.md` → `done`:

- [ ] All acceptance criteria reflected (checked in `task.md`)
- [ ] `verification.md` covers each `required_live_evidence` or documents allowed gap in contract
- [ ] `findings.md` or `findings-*.md` for non-obvious discoveries
- [ ] `sources.md` if external sources or code references were used
- [ ] `plan.md` steps match what was actually done
- [ ] Reusable lookup knowledge promoted to an index, skill, or rule, or explicitly kept task-local

## Child agents

Include in delegation prompt:

```text
Update task artifacts per skills/task-artifacts/SKILL.md after each major step.
Required before you finish: verification.md for all live smokes; findings for research.
```
