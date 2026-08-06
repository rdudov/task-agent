#!/usr/bin/env bash
# Rename a task directory, keeping its ID.
#
# Thin wrapper: tasks_index.py performs the move, rewrites the `slug` field in
# the `slug` line in the task's frontmatter and updates the lookup index. The
# task keeps its number, which is its identity, so cross-task references by
# number survive the rename.
#
# The CLI signature is unchanged, so existing callers and skills do not change.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <task-dir> <new-slug>" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/python_runtime.sh"
python_bin="$(task_agent_python)"
exec "$python_bin" "$script_dir/tasks_index.py" rename "$1" "$2"
