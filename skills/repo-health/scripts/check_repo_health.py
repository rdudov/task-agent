#!/usr/bin/env python3
from __future__ import annotations

import argparse
import py_compile
import re
import sys
from pathlib import Path


ROOT_FILES = [
    "AGENTS.md",
    "README.md",
    ".gitignore",
    "requirements.txt",
    "requirements.lock",
    "docs/architecture.md",
    "docs/task-execution.md",
    "docs/claude-code-setup.md",
    "CLAUDE.md",
]

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"\b(?:OPENAI|ANTHROPIC|GITHUB|TELEGRAM|GOOGLE|GMAIL|SLACK|DISCORD)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET|HASH)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{40,})\b"),
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def check_skill_manifest(path: Path) -> list[str]:
    errors: list[str] = []
    text = read_text(path)
    if not text.startswith("---\n"):
        return [f"{path}: missing YAML-style frontmatter"]
    end = text.find("\n---", 4)
    if end == -1:
        return [f"{path}: frontmatter is not closed"]
    frontmatter = text[4:end]
    for field in ("name:", "description:"):
        if field not in frontmatter:
            errors.append(f"{path}: missing {field} in frontmatter")
    return errors


def check_tasks(root: Path, allow_empty_tasks: bool) -> list[str]:
    errors: list[str] = []
    tasks_dir = root / "tasks"
    if not tasks_dir.exists():
        if allow_empty_tasks:
            return errors
        return ["tasks/: missing task artifact directory"]

    index = tasks_dir / "INDEX.md"
    example_index = tasks_dir / "INDEX.example.md"
    if not index.exists():
        task_dirs = sorted(path for path in tasks_dir.iterdir() if path.is_dir())
        if allow_empty_tasks and not task_dirs and example_index.exists():
            return errors
        errors.append("tasks/INDEX.md: missing canonical task index")
    elif not index.read_text(encoding="utf-8", errors="replace").strip():
        errors.append("tasks/INDEX.md: empty canonical task index")

    task_dirs = sorted(path for path in tasks_dir.iterdir() if path.is_dir())
    if not task_dirs and not allow_empty_tasks:
        errors.append("tasks/: no task directories found")

    for task_dir in task_dirs:
        for required in ("task.md", "plan.md"):
            if not (task_dir / required).exists():
                errors.append(f"{task_dir.relative_to(root)}: missing {required}")

    if index.exists():
        for match in re.finditer(r"\]\(([^)]+/task\.md)\)", read_text(index)):
            link = match.group(1)
            target = (tasks_dir / link).resolve()
            try:
                target.relative_to(tasks_dir.resolve())
            except ValueError:
                errors.append(f"tasks/INDEX.md: task link escapes tasks/: {link}")
                continue
            if not target.exists():
                errors.append(f"tasks/INDEX.md: broken task link {link}")

    return errors


def check_scripts(root: Path) -> list[str]:
    errors: list[str] = []
    for script in sorted((root / "skills").glob("*/scripts/*.py")):
        try:
            py_compile.compile(str(script), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{script.relative_to(root)}: Python syntax check failed: {exc.msg}")
    return errors


def check_agent_entry_points(root: Path) -> list[str]:
    """Check that Claude Code still reaches the canonical rules and skills.

    Both ways this wiring breaks are silent: Claude Code ignores an `@` import
    that does not end in `.md`, and it skips a dangling symlink without a
    warning. A session then runs with no project rules at all and nothing says
    so, which is exactly the failure this check exists to make loud.
    """
    errors: list[str] = []
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        return errors

    imports = set(re.findall(r"^@(\S+)$", read_text(claude_md), flags=re.MULTILINE))
    for imported in sorted(imports):
        if not imported.endswith(".md"):
            errors.append(f"CLAUDE.md: import is ignored unless it ends in .md: @{imported}")
            continue
        target = root / imported
        if not target.exists():
            errors.append(f"CLAUDE.md: imported path does not resolve: @{imported}")

    rules_dir = root / ".cursor" / "rules"
    if rules_dir.exists():
        for rule in sorted(rules_dir.glob("*.mdc")):
            link = root / ".claude" / "imports" / f"{rule.stem}.md"
            if not link.exists():
                errors.append(
                    f".claude/imports/{rule.stem}.md: missing symlink, so {rule.name} "
                    "never reaches Claude Code"
                )
            elif f".claude/imports/{rule.stem}.md" not in imports:
                errors.append(
                    f"CLAUDE.md: no import line for .claude/imports/{rule.stem}.md, "
                    f"so {rule.name} never loads"
                )

    skills_link = root / ".claude" / "skills"
    if skills_link.is_symlink() and not skills_link.resolve().exists():
        errors.append(".claude/skills: dangling symlink, so repository skills never load")
    elif (root / "skills").exists() and not skills_link.exists():
        errors.append(".claude/skills: missing symlink to skills/, so no skill is discoverable")

    return errors


def check_secret_like_content(root: Path) -> list[str]:
    errors: list[str] = []
    for base in (root / "tasks", root / "data"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            try:
                text = read_text(path)
            except OSError:
                continue
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    errors.append(f"{path.relative_to(root)}: possible secret-like content")
                    break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Run structural health checks for the task-agent repository.")
    parser.add_argument("--allow-empty-tasks", action="store_true", help="Allow templates without local task history.")
    args = parser.parse_args()

    root = repo_root()
    errors: list[str] = []

    for rel_path in ROOT_FILES:
        if not (root / rel_path).exists():
            errors.append(f"{rel_path}: missing required file")

    skills_dir = root / "skills"
    if not skills_dir.exists():
        errors.append("skills/: missing skills directory")
    else:
        manifests = sorted(skills_dir.glob("*/SKILL.md"))
        if not manifests:
            errors.append("skills/: no skill manifests found")
        for manifest in manifests:
            errors.extend(check_skill_manifest(manifest))

    errors.extend(check_tasks(root, allow_empty_tasks=args.allow_empty_tasks))
    errors.extend(check_agent_entry_points(root))
    errors.extend(check_scripts(root))
    errors.extend(check_secret_like_content(root))

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Repository health checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
