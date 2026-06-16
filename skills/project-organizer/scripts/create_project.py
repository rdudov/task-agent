#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a durable project record under data/projects/."
    )
    parser.add_argument("--title", required=True, help="Project title.")
    parser.add_argument("--slug", required=True, help="Project slug.")
    parser.add_argument(
        "--status",
        default="active",
        help="Initial project status. Default: active.",
    )
    parser.add_argument(
        "--summary",
        default="",
        help="Optional one-line project summary.",
    )
    parser.add_argument(
        "--with-context",
        action="store_true",
        help="Create an empty context.md file.",
    )
    parser.add_argument(
        "--with-status",
        action="store_true",
        help="Create a status.md file with a rolling project-status template.",
    )
    parser.add_argument(
        "--with-decisions",
        action="store_true",
        help="Create an empty decisions.md file.",
    )
    parser.add_argument(
        "--with-sources",
        action="store_true",
        help="Create an empty sources.md file.",
    )
    parser.add_argument(
        "--with-artifacts-dir",
        action="store_true",
        help="Create an empty artifacts/ directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[3]
    project_dir = repo_root / "data" / "projects" / args.slug
    project_file = project_dir / "project.md"

    if project_dir.exists():
        raise SystemExit(f"Project directory already exists: {project_dir}")

    project_dir.mkdir(parents=True)

    summary = args.summary or (
        f"Durable project record for {args.title.lower()} across multiple tasks."
    )
    project_file.write_text(
        "\n".join(
            [
                f"# {args.title}",
                "",
                "## Status",
                args.status,
                "",
                "## Summary",
                summary,
                "",
                "## Related Tasks",
                "- none",
                "",
                "## Notes",
                "- none",
                "",
            ]
        ),
        encoding="utf-8",
    )

    optional_files = {
        "status.md": args.with_status,
        "context.md": args.with_context,
        "decisions.md": args.with_decisions,
        "sources.md": args.with_sources,
    }
    for name, enabled in optional_files.items():
        if enabled:
            if name == "status.md":
                (project_dir / name).write_text(
                    "\n".join(
                        [
                            "# Status",
                            "",
                            "## Snapshot",
                            "- Updated: YYYY-MM-DD",
                            "- State: active",
                            "- Overall: summarize the current project state.",
                            "",
                            "## Completed Work",
                            "- none",
                            "",
                            "## Newly Added Outcomes",
                            "- none",
                            "",
                            "## Remaining Work",
                            "- none",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                (project_dir / name).write_text(
                    f"# {name.removesuffix('.md').replace('-', ' ').title()}\n",
                    encoding="utf-8",
                )

    if args.with_artifacts_dir:
        (project_dir / "artifacts").mkdir()

    print(project_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
