# User Preferences

Copy this file to `tasks/USER_PREFERENCES.md` to start recording durable
preferences. The real file is local state, like the rest of `tasks/`.

This record answers one question: when the user did **not** say how they want
something, what should the agent default to? It is not a place for task
requirements.

## Rules For Agents

Read this file before choosing any unspecified output representation, tone, or
delivery format.

Precedence, strongest first:

1. the current request
2. any later continuation in the same task
3. this file
4. the agent's own default

Write to this file only from an explicit, reusable instruction: the user says
remember, always, never, by default, or from now on. Record the preference, the
task it came from, and the date. One-off task requirements never become
defaults — a user who asks for a PDF once has not asked for PDFs forever.

Remove or amend an entry when the user contradicts it. A stale preference is
worse than a missing one, because it silently overrides the agent's judgment.

Never infer a preference from prose, from repetition, or from what the user
seemed to like. Inference is how a single convenient choice becomes a permanent
rule nobody asked for.

## Output Formats

<!-- Example, delete when you add real entries:
- Default deliverable format is DOCX, not Markdown.
  Source: task 012, 2026-03-04, "always send me documents as docx from now on".
-->

## Language And Tone

<!-- Example:
- Reply in Russian; keep code, identifiers, and commit messages in English.
  Source: task 007, 2026-02-19.
-->

## Delivery And Notification

<!-- Example:
- Do not send interim progress notifications for runs under ten minutes.
  Source: task 031, 2026-05-02.
-->

## Working Style

<!-- Example:
- Never push to a shared base branch; always open a branch for review.
  Source: task 044, 2026-06-11.
-->
