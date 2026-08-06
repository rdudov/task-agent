#!/usr/bin/env bash
# Create a task directory under tasks/.
#
# Thin wrapper: tasks_index.py owns ID allocation, the task.md/plan.md
# templates, the frontmatter and the lookup index. Keeping
# the allocation inside a single SQLite transaction is what stops two
# concurrent child agents from receiving the same number, which is how the
# pre-2026-07-27 find|sed|sort|tail version produced 23 duplicate IDs.
#
# The CLI signature is unchanged, so existing callers and skills do not change.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <title> <summary> [slug] [--project path]... [--trip path]..." >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/tasks_index.py" add "$@"
