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
| Done | `task.md` | Status `done`; acceptance criteria `[x]` |

Runner-managed (when using task-runner): `trace.md`, `status.json` — keep in sync with the table above.

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

## Checkpoint triggers (do not skip)

1. Finished a plan step with external dependency → update `plan.md` + one line in `trace.md` or `verification.md`
2. Ran any smoke script → append `verification.md` immediately
3. Discovered scope change → update `task.md` Open Questions / Acceptance Criteria
4. Parent delegated to child → child must return with updated artifacts; parent verifies files exist before closing

## Helper script

Append a verification section without hand-editing headers:

```bash
skills/task-artifacts/scripts/record_verification.sh tasks/002-example \
  "agent_smoke" \
  "Full CLI run OK; trace: Grep turn 1; no secrets in log."
```

## Completion checklist

Before `task.md` → `done`:

- [ ] All acceptance criteria reflected (checked in `task.md`)
- [ ] `verification.md` covers each `required_live_evidence` or documents allowed gap in contract
- [ ] `findings.md` or `findings-*.md` for non-obvious discoveries
- [ ] `sources.md` if external sources or code references were used
- [ ] `plan.md` steps match what was actually done

## Child agents

Include in delegation prompt:

```text
Update task artifacts per skills/task-artifacts/SKILL.md after each major step.
Required before you finish: verification.md for all live smokes; findings for research.
```
