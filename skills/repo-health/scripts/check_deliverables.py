#!/usr/bin/env python3
"""Validate a task's registered deliverables against the delivery contract.

The manifest is the authority on what a task delivers. This checker enforces the
part of that contract a template can enforce locally: registration is explicit,
every registered basename resolves to a contained regular file with content, and
nothing is registered twice or beyond the configured limits.

What it deliberately does not do is decide *which* files should have been
requested. Only the agent that read the request knows that.
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULT_MAX_FILES = 20
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

# Internal task records. Registering one is legitimate only when the user asked
# for that record itself, so this is a warning rather than a failure.
INTERNAL_RECORDS = {
    "findings.md",
    "verification.md",
    "sources.md",
    "trace.md",
    "task.md",
    "plan.md",
    "status.json",
    "progress.json",
    "publication.json",
    "task_contract.json",
}


def check_deliverables(
    task_dir: Path,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """Return a structured verdict for one task directory."""
    errors: list[str] = []
    warnings: list[str] = []
    deliverables_dir = task_dir / "deliverables"
    manifest_path = deliverables_dir / "manifest.json"

    if not manifest_path.exists():
        if deliverables_dir.exists() and any(deliverables_dir.iterdir()):
            errors.append(
                f"{deliverables_dir} has files but no manifest.json; "
                "delivery is driven by explicit registration, not by directory contents"
            )
        return {
            "task_dir": str(task_dir),
            "manifest": str(manifest_path),
            "registered": [],
            "total_bytes": 0,
            "errors": errors,
            "warnings": warnings,
            "ok": not errors,
        }

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{manifest_path} is not readable JSON: {exc}")
        return {
            "task_dir": str(task_dir),
            "manifest": str(manifest_path),
            "registered": [],
            "total_bytes": 0,
            "errors": errors,
            "warnings": warnings,
            "ok": False,
        }

    if not isinstance(manifest, dict):
        errors.append(f"{manifest_path} must contain a JSON object")
        entries: list = []
    else:
        entries = manifest.get("deliverables", [])
        if not isinstance(entries, list):
            errors.append(f"{manifest_path} `deliverables` must be an array")
            entries = []

    resolved_root = deliverables_dir.resolve(strict=False)
    seen: set[str] = set()
    registered: list[str] = []
    total_bytes = 0

    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"Registered entry is not a non-empty string: {entry!r}")
            continue
        if entry in seen:
            errors.append(f"Registered twice: {entry}")
            continue
        seen.add(entry)

        if entry != Path(entry).name or entry in {".", ".."}:
            errors.append(f"Registered entry must be a bare basename: {entry}")
            continue

        candidate = deliverables_dir / entry
        if candidate.is_symlink():
            errors.append(f"Registered entry is a symlink: {entry}")
            continue
        if not candidate.exists():
            errors.append(f"Registered entry does not exist: {entry}")
            continue
        if not candidate.is_file():
            errors.append(f"Registered entry is not a regular file: {entry}")
            continue
        try:
            if candidate.resolve(strict=True).parent != resolved_root:
                errors.append(f"Registered entry escapes the deliverables directory: {entry}")
                continue
        except OSError as exc:
            errors.append(f"Cannot resolve registered entry {entry}: {exc}")
            continue

        size = candidate.stat().st_size
        if size == 0:
            errors.append(f"Registered entry is empty: {entry}")
            continue

        if entry in INTERNAL_RECORDS:
            warnings.append(
                f"{entry} is an internal task record; register it only when the user "
                "asked for that record itself"
            )

        total_bytes += size
        registered.append(entry)

    if len(registered) > max_files:
        errors.append(f"Registered {len(registered)} files, limit is {max_files}")
    if total_bytes > max_bytes:
        errors.append(f"Registered {total_bytes} bytes, limit is {max_bytes}")

    present = {
        item.name
        for item in deliverables_dir.iterdir()
        if item.name != "manifest.json"
    } if deliverables_dir.exists() else set()
    unregistered = sorted(present - seen)
    if unregistered:
        warnings.append(
            "Present but not registered, so not delivered: " + ", ".join(unregistered)
        )

    return {
        "task_dir": str(task_dir),
        "manifest": str(manifest_path),
        "registered": registered,
        "total_bytes": total_bytes,
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate registered task deliverables against the delivery contract.",
    )
    parser.add_argument("task_dir", help="Task directory containing deliverables/manifest.json.")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--json", action="store_true", help="Emit the verdict as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task_dir = Path(args.task_dir).expanduser()
    if not task_dir.is_dir():
        print(f"Task directory does not exist: {task_dir}", file=sys.stderr)
        return 2

    verdict = check_deliverables(task_dir, args.max_files, args.max_bytes)

    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        for warning in verdict["warnings"]:
            print(f"WARN: {warning}")
        for error in verdict["errors"]:
            print(f"FAIL: {error}", file=sys.stderr)
        if verdict["ok"]:
            count = len(verdict["registered"])
            print(f"Deliverables check passed: {count} registered, {verdict['total_bytes']} bytes.")

    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
