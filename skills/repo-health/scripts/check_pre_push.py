#!/usr/bin/env python3
"""Generic pre-push guardrail for task-agent workspaces."""
from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
from pathlib import Path


FORBIDDEN_PATHS = {
    ".env",
}

FORBIDDEN_PREFIXES = (
    "tasks/",
    "data/",
    ".state/",
)

# Skeleton files the template intentionally tracks inside otherwise-local trees.
# Keep this in sync with the matching `!` exceptions in .gitignore: a new tracked
# skeleton file that is missing here fails the pre-push check.
ALLOWED_TEMPLATE_ARTIFACTS = {
    "tasks/.gitkeep",
    "tasks/INDEX.example.md",
    "tasks/USER_PREFERENCES.example.md",
    "data/.gitkeep",
    "data/local-projects.example.md",
    "data/projects/.gitkeep",
}

FORBIDDEN_SUFFIXES = (
    "/.env",
    ".pem",
    ".key",
)

class EncodedLiteralPattern:
    """Regex-compatible matcher whose guard literals do not leak into source."""

    def __init__(self, *encoded_literals: str) -> None:
        self.literals = tuple(
            base64.b64decode(value).decode("utf-8") for value in encoded_literals
        )

    def search(self, text: str):
        return next((literal for literal in self.literals if literal in text), None)


SECRET_PATTERNS: list[tuple[str, object]] = [
    (
        "private task/project history",
        EncodedLiteralPattern(
            "bW9leC1zdHJhdGVneS1sYWI=",
            "Z3JlZW5maWVsZC1kZWVwcmVzZWFyY2g=",
            "NDIzLXJ1bi1maXJzdC1zaXgtbW9udGgtbW9leC1zdHJhdGVneS1jeWNsZQ==",
            "MjAyNi0wNS0wMi10dXJrZXktZmxvdGlsbGE=",
            "Y29tcGFuaW9uLWFnZW50IHJvb3Q=",
        ),
    ),
    ("private key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    (
        "secret assignment",
        re.compile(
            r"\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|HASH)"
            r"[ \t]*[:=][ \t]*['\"]?[A-Za-z0-9_./+=:-]{12,}"
        ),
    ),
    ("api token", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{40,})\b")),
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def run_git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git failed").strip())
    return proc.stdout


def remote_url(remote: str, root: Path) -> str:
    return run_git(["remote", "get-url", remote], root).strip()


def default_base(remote: str, root: Path) -> str | None:
    candidates: list[str] = []
    try:
        head_ref = run_git(["rev-parse", "--abbrev-ref", f"{remote}/HEAD"], root).strip()
        if head_ref:
            candidates.append(head_ref)
    except RuntimeError:
        pass
    candidates.extend([f"{remote}/main", f"{remote}/master"])

    for candidate in candidates:
        try:
            run_git(["rev-parse", "--verify", candidate], root)
        except RuntimeError:
            continue
        return candidate
    return None


def outgoing_files(remote: str, root: Path, base: str | None) -> list[str]:
    base_ref = base or default_base(remote, root)
    if base_ref:
        out = run_git(["diff", "--name-only", f"{base_ref}...HEAD"], root).strip()
        return sorted({line for line in out.splitlines() if line})

    staged = run_git(["diff", "--cached", "--name-only"], root).strip()
    if staged:
        return sorted({line for line in staged.splitlines() if line})

    out = run_git(["show", "--name-only", "--pretty=format:", "HEAD"], root).strip()
    return sorted({line for line in out.splitlines() if line})


def unexpected_refs(remote: str, root: Path) -> list[str]:
    """Return refs that a publishing clone must not retain or mirror."""
    refs = run_git(["for-each-ref", "--format=%(refname)"], root).splitlines()
    allowed = ("refs/heads/", f"refs/remotes/{remote}/")
    return sorted(ref for ref in refs if ref and not ref.startswith(allowed))


def is_forbidden_path(path: str, allow_local_artifacts: bool) -> bool:
    if path in ALLOWED_TEMPLATE_ARTIFACTS:
        return False
    if path in FORBIDDEN_PATHS:
        return True
    if path.endswith(".env") and not path.endswith(".env.example"):
        return True
    if any(path.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return True
    if allow_local_artifacts:
        return False
    return any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)


def scan_file(root: Path, rel_path: str, allow_local_artifacts: bool) -> list[str]:
    errors: list[str] = []
    full = root / rel_path
    if not full.exists():
        return errors

    if is_forbidden_path(rel_path, allow_local_artifacts=allow_local_artifacts):
        errors.append(f"{rel_path}: forbidden local/private path in outgoing commits")
        return errors

    if not full.is_file() or full.stat().st_size > 2_000_000:
        return errors

    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{rel_path}: cannot read ({exc})"]

    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.append(f"{rel_path}: possible leak ({label})")
            break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Check outgoing git changes for local artifacts and likely secrets.")
    parser.add_argument("--remote", default="origin", help="Remote name to inspect.")
    parser.add_argument("--base", help="Base ref to diff against, such as origin/main.")
    parser.add_argument(
        "--allow-local-artifacts",
        action="store_true",
        help="Allow tasks/, data/, and .state/ paths when intentionally publishing a template artifact change.",
    )
    args = parser.parse_args()

    root = repo_root()
    try:
        url = remote_url(args.remote, root)
        files = outgoing_files(args.remote, root, args.base)
        extra_refs = unexpected_refs(args.remote, root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not files and not extra_refs:
        print(f"Pre-push check passed for {args.remote} ({url}): no outgoing files.")
        return 0

    errors: list[str] = []
    errors.extend(f"{ref}: unexpected ref in publishing clone" for ref in extra_refs)
    for rel_path in files:
        errors.extend(scan_file(root, rel_path, allow_local_artifacts=args.allow_local_artifacts))

    if errors:
        print(f"Pre-push check FAILED for {args.remote} ({url}):", file=sys.stderr)
        for error in errors:
            print(f"  ERROR: {error}", file=sys.stderr)
        print(f"  Scanned {len(files)} file(s) in outgoing commits.", file=sys.stderr)
        return 1

    print(f"Pre-push check passed for {args.remote} ({url}); scanned {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
