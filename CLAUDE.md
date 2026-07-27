# Claude Code Entry Point

This file exists so Claude Code loads the same operating rules that Codex and
Cursor already use. It deliberately contains no rules of its own: every rule
below is imported from its canonical file, so there is exactly one source of
truth per rule and nothing is duplicated per agent.

## Project rules

@AGENTS.md

## Always-on rules

These are the Cursor rules. Each path below is a symlink to the canonical
`.cursor/rules/*.mdc` file; the content lives there and only there.

@.claude/imports/task-bootstrap-required.md
@.claude/imports/context-discovery.md
@.claude/imports/git-remote-sync.md
@.claude/imports/git-push-safety.md
@.claude/imports/task-artifact-updates.md

## Repository wiring for Claude Code

- `AGENTS.md` is the canonical project rule file, shared with Codex. It is
  imported above rather than copied.
- `.cursor/rules/*.mdc` are the canonical rule files, shared with Cursor. They
  are imported above through `.claude/imports/*.md` symlinks rather than copied.
  Claude Code applies all of them on every session; Cursor additionally scopes
  `task-artifact-updates` by its `globs` frontmatter, which Claude Code ignores.
- `skills/` is the canonical skill library. It is exposed to Claude Code through
  the `.claude/skills` symlink, so repository skills appear as normal Agent
  Skills. Invoke them by name instead of copying their instructions.
- Environment-specific rules do not belong in this committed file. Keep them in
  a gitignored `CLAUDE.local.md`.

Edit the canonical files, never the symlinks, and never move a canonical file
into `.claude/`. Codex reads `AGENTS.md` from the repository root, and Cursor
reads `.cursor/rules/`.

`.claude/imports/` exists only because Claude Code imports files by the `.md`
extension and silently ignores an `@…​.mdc` import. Import resolution does follow
symlinks, so a `.md` symlink to the `.mdc` original carries the rule without
duplicating it. The directory holds nothing but those symlinks.

[docs/claude-code-setup.md](docs/claude-code-setup.md) explains how to recreate,
change, or verify this wiring.

## Claude-specific notes

- `skills/task-runner/scripts/task_runner.py` supports `--runner codex`,
  `--runner claude`, and `--runner agent`, and resolves the child from the parent
  CLI agent when no flag is given. Delegating a task from a Claude session
  launches a Claude child. `skills/task-runner/SKILL.md` documents the resolution
  order and the per-runner access mapping.
- A Claude session governs its own access through permission modes and
  `.claude/settings.json`. Use `--add-dir` when a task legitimately needs a
  sibling repository outside this checkout.
- Do not substitute Claude subagents for a launched child CLI process without an
  explicit task that changes that architecture. That applies to the standard
  workflow and to `--workflow dev-pipeline`, whose owner is a real CLI session.
