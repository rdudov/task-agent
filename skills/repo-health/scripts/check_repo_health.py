#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
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
]


def _load_pre_push():
    """Reuse the leak-guard's pattern definitions instead of keeping a copy.

    "What looks like a leaked secret" is one concept and the two scripts in this
    skill disagreed about it. The copy here had `\\s*` where `check_pre_push.py`
    has `[ \\t]*`, so `EXAMPLE_API_TOKEN=` with an *empty* value matched across
    the newline and reported the *next* variable's name as the secret -- which is
    how a committed `.env.example` template became two of the fifteen errors that
    blocked every task's closure.
    """
    path = Path(__file__).resolve().parent / "check_pre_push.py"
    spec = importlib.util.spec_from_file_location("check_pre_push", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load the leak-guard pattern owner: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRE_PUSH = _load_pre_push()
SECRET_PATTERNS = PRE_PUSH.SECRET_PATTERNS


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

    # `.state/tasks-index.db` is one table rebuilt from `tasks/` on demand, so
    # neither its staleness nor its absence is a health problem: the next command
    # builds it again, task numbers included. What still matters structurally is
    # that every task directory carries its artifacts; `tasks_index.py check`
    # owns the task-metadata properties.
    # Hidden names are not tasks. `tasks_index.py` skips them because that is
    # where `add` stages a directory before publishing it, and a stray
    # `tasks/.claude` otherwise reports as a task missing both its artifacts.
    task_dirs = sorted(path for path in tasks_dir.iterdir()
                       if path.is_dir() and not path.name.startswith("."))
    if not task_dirs and not allow_empty_tasks:
        errors.append("tasks/: no task directories found")

    for task_dir in task_dirs:
        for required in ("task.md", "plan.md"):
            if not (task_dir / required).exists():
                errors.append(f"{task_dir.relative_to(root)}: missing {required}")

    return errors


def check_scripts(root: Path) -> list[str]:
    """Parse every skill script, without writing anything into the repository.

    This used to call `py_compile.compile`, which writes a `__pycache__` entry
    next to each script as a side effect. A health check that mutates the tree it
    is inspecting cannot run over a read-only checkout at all -- it aborts on the
    first `OSError` instead of reporting on the repository. `compile()` answers
    the same question, "does this parse", and answers it in memory.
    """
    errors: list[str] = []
    for script in sorted((root / "skills").glob("*/scripts/*.py")):
        try:
            compile(script.read_bytes(), str(script), "exec")
        except SyntaxError as exc:
            errors.append(f"{script.relative_to(root)}: Python syntax check failed: {exc}")
        except (OSError, ValueError) as exc:
            errors.append(f"{script.relative_to(root)}: cannot be read for the syntax check: {exc}")
    return errors


def scannable_paths(root: Path) -> list[str]:
    """The repository content Git would carry, as repository-relative paths.

    `git ls-files --cached --others --exclude-standard` is exactly "tracked, plus
    what is not tracked yet and not ignored": the files a task actually adds to
    or changes in the repository. Raises when the scope cannot be established.
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git ls-files failed").strip())
    return [item for item in proc.stdout.split("\0") if item]


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
            rel = f".claude/imports/{rule.stem}.md"
            link = root / ".claude" / "imports" / f"{rule.stem}.md"
            if not link.exists() and not link.is_symlink():
                errors.append(
                    f"{rel}: missing symlink, so {rule.name} never reaches Claude Code"
                )
            elif not link.is_symlink():
                # A real file here would load, which is exactly the problem: it is
                # a second copy of the rule, free to drift from the canonical one.
                errors.append(
                    f"{rel}: must be a symlink to ../../.cursor/rules/{rule.name}, "
                    "not a copy of the rule"
                )
            elif link.resolve() != rule.resolve():
                errors.append(
                    f"{rel}: resolves to {link.resolve()} instead of "
                    f".cursor/rules/{rule.name}"
                )
            elif rel not in imports:
                errors.append(
                    f"CLAUDE.md: no import line for {rel}, so {rule.name} never loads"
                )

    skills_dir = root / "skills"
    skills_link = root / ".claude" / "skills"
    if skills_dir.exists():
        if not skills_link.exists() and not skills_link.is_symlink():
            errors.append(
                ".claude/skills: missing symlink to skills/, so no skill is discoverable"
            )
        elif not skills_link.is_symlink():
            errors.append(
                ".claude/skills: must be a symlink to ../skills, not a separate directory"
            )
        elif not skills_link.resolve().exists():
            errors.append(".claude/skills: dangling symlink, so repository skills never load")
        elif skills_link.resolve() != skills_dir.resolve():
            errors.append(
                f".claude/skills: resolves to {skills_link.resolve()} instead of skills/"
            )

    return errors



def check_secret_like_content(root: Path) -> list[str]:
    """Scan the tracked tree, which is the only content this repository publishes.

    It used to walk `tasks/` and `data/` instead. Both are listed in `.gitignore`
    in full, so that scan covered zero publishable bytes and every byte of the
    durable local artifacts -- including finished snapshots of unrelated tasks,
    their `__pycache__`, their captured `runner.log`, and the `.env.example`
    files they copied in. A product run reported fifteen errors that way, none of
    them from the tree under change, which blocked the closure of *every* task
    rather than of the one that introduced something. The scope is now what the
    task really changes; the strictness is unchanged, and `check_pre_push.py`
    still refuses `tasks/` and `data/` outright in outgoing commits.
    """
    errors: list[str] = []
    try:
        candidates = scannable_paths(root)
    except (RuntimeError, OSError) as exc:
        # A leak gate that silently scans nothing is worse than no gate. Say the
        # scope could not be established rather than pass by default.
        return [f"repository scope for the secret scan cannot be established: {exc}"]
    for relative in candidates:
        path = root / relative
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = read_text(path)
        except OSError:
            continue
        if PRE_PUSH.private_history_match(text, root):
            errors.append(
                f"{relative}: possible secret-like content (private task/project history)"
            )
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"{relative}: possible secret-like content ({label})")
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
