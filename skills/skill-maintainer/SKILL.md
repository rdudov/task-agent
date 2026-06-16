---
name: skill-maintainer
description: Use this skill when creating, reviewing, restoring, or changing repository skills. It keeps skill behavior self-contained, validates manifests, and coordinates required project documentation updates for skill-system changes.
---

# Skill Maintainer

This skill maintains the repository's skill system.

## Scope

Use it when:

- adding a new `skills/<name>/` directory
- changing how skills are discovered, invoked, or validated
- restoring skills after backup loss
- moving behavior between project docs and skill-specific docs
- preparing a public template that should keep only generic skills

## Skill Structure

Every skill must have:

- `SKILL.md`
- YAML-style frontmatter with `name` and `description`
- a concise scope section explaining when to use the skill

Optional files:

- `scripts/` for deterministic commands the agent should run instead of retyping large logic
- `references/` for supporting docs that are too detailed for the main skill file
- `tests/` when scripts or parsing logic have behavior worth locking down

## Maintenance Rules

- Keep skill-specific instructions in that skill's `SKILL.md`.
- Keep project-wide conventions in `AGENTS.md` or `docs/`.
- Do not copy private project instructions into generic skills.
- If a change affects task lifecycle, task artifacts, skill discovery, skill execution, agent orchestration, restore behavior, or resume behavior, update project docs in the same source change.
- If a skill writes durable data, document whether that data belongs in `tasks/`, `data/`, or runtime state such as `.state/`.

## Verification

After changing skills, run:

```bash
.venv/bin/python skills/repo-health/scripts/check_repo_health.py
```

Run skill-specific tests when present, for example:

```bash
PYTHONPATH=skills/task-runner/scripts .venv/bin/python -m pytest skills/task-runner/tests
```

## Publishing A Generic Template

When preparing a reusable public copy:

- keep core skills that implement task lifecycle and delegation
- remove or clearly separate personal integrations
- do not publish local `tasks/`, `data/`, `.state/`, logs, credentials, or private runtime config
- include sanitized examples only when they teach the generic workflow
