# Claude Code Setup

This repository's operating rules were written for Codex and Cursor. Claude Code
reads neither `AGENTS.md` nor `.cursor/rules/`, and it discovers skills only under
`.claude/skills/`. Without the wiring described here, a `claude` session in this
repository runs with **no project rules and no repository skills at all**.

The wiring principle is that nothing is copied. Every rule keeps exactly one
canonical file; Claude Code reaches it through an import or a symlink.

## What Is Wired

| Canonical source | Read by | Reaches Claude Code through |
| --- | --- | --- |
| `AGENTS.md` | Codex | `CLAUDE.md` → `@AGENTS.md` |
| `.cursor/rules/*.mdc` | Cursor | `CLAUDE.md` → `@.claude/imports/*.md` symlinks |
| `skills/*/SKILL.md` | this repository's convention | `.claude/skills` → `../skills` symlink |
| environment-specific rules | local-only, gitignored | `CLAUDE.local.md` |

`CLAUDE.md` and `.claude/` are committed. `CLAUDE.local.md` and
`.claude/settings.local.json` are gitignored.

## Discovery Rules Worth Knowing

These explain why the wiring looks the way it does:

- Claude Code does not load `AGENTS.md` on its own. Only `CLAUDE.md`,
  `CLAUDE.local.md`, and `.claude/rules/` are project memory.
- An `@path` import is resolved **only for `.md` files**. `@.cursor/rules/foo.mdc`
  is ignored silently, with no warning and no error.
- Import resolution **does follow symlinks**. This is what makes
  `.claude/imports/foo.md` → `../../.cursor/rules/foo.mdc` work: the extension gate
  is satisfied by the link name, and the content comes from the canonical `.mdc`.
- The `.claude/rules/` scanner **does not follow symlinked files**. A real file
  there is loaded; a symlink to one elsewhere is skipped, again silently. That is
  why the Cursor rules are imported rather than placed in `.claude/rules/`.
- A symlinked skill *directory* is followed. `.claude/skills` → `../skills`
  exposes the whole skill library with one link.
- Existing `skills/*/SKILL.md` frontmatter (`name`, `description`) already matches
  the Agent Skills format. No skill file needs changes.

Because both failure modes above are silent, verify with a live probe after
touching the wiring rather than assuming a file loaded.

## Recreating The Wiring From Scratch

```bash
cd /path/to/task-agent

# skills
mkdir -p .claude
ln -sfn ../skills .claude/skills

# cursor rules: .md symlinks that satisfy the import extension gate
mkdir -p .claude/imports
for f in .cursor/rules/*.mdc; do
  n=$(basename "$f" .mdc)
  ln -sfn "../../.cursor/rules/$n.mdc" ".claude/imports/$n.md"
done
```

Then make sure `CLAUDE.md` imports `@AGENTS.md` plus every
`@.claude/imports/<rule>.md`.

## Verifying The Wiring

Run these from the repository root. Each probe is a separate Claude Code process,
so it proves what a fresh session actually loads, not what the current session
remembers.

```bash
claude -p --model haiku "Do not use any tools. Answer only from instructions already in your context, one line each:
1) AGENTS: the path this project calls the canonical ordered task index, or NONE
2) BOOTSTRAP: complete 'Chat does not replace ...' from the task bootstrap rule, or NONE
3) PUSHSAFETY: the script path the git push safety rule tells you to run, or NONE"

claude -p --model haiku "Do not use any tools. List the names of any skills available to you that come from this repository. Comma-separated, or NONE."
```

Expected: `tasks/INDEX.md`; the `task.md, plan.md, findings.md, or
verification.md` list; `skills/repo-health/scripts/check_pre_push.py`; and a skill
list containing `task-runner`, `task-creator`, `task-artifacts`, `repo-health`,
and the rest of `skills/`.

A `NONE` anywhere means that wiring is broken. Check the symlink targets first
(`ls -la .claude/skills .claude/imports`), then check that the import lines in
`CLAUDE.md` end in `.md`.

## Adding A New Rule Or Skill

- **New Cursor rule**: write `.cursor/rules/<name>.mdc` as usual, then add the
  symlink in `.claude/imports/` and the matching `@` line in `CLAUDE.md`. A rule
  that is not imported is invisible to Claude Code.
- **New skill**: create `skills/<name>/SKILL.md` as usual through
  `skills/skill-maintainer/`. The `.claude/skills` symlink covers the whole
  directory, so no per-skill wiring is needed.
- **New project rule**: edit `AGENTS.md`. It is imported wholesale.

Edit canonical files only. Never edit through a symlink target path in
`.claude/`, and never move a canonical file into `.claude/`, or Codex and Cursor
lose it.

## Claude As A Child Runner

The wiring above makes Claude Code read the same rules in an interactive session.
Launching Claude as a *child* agent is a separate mechanism, documented in
`skills/task-runner/SKILL.md`:

- `task_runner.py` accepts `--runner codex`, `--runner claude`, and
  `--runner agent`. With no explicit flag it resolves the child from the parent
  CLI agent, so a Claude session launches a Claude child.
- `--workflow dev-pipeline` accepts `--runner codex` and `--runner claude`,
  because those are the owner runtimes the dev-pipeline core drives.
- Access level is expressed once through `--sandbox-mode` and mapped per runner.
  The Claude mapping needs a Linux host with `bubblewrap` and `socat` for its
  restricted modes and fails closed without them.
- A Claude child always keeps the repository as its working directory, so
  `CLAUDE.md` and everything it imports load automatically.
