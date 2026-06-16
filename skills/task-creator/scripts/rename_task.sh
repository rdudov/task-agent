#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <task-dir> <new-slug>" >&2
  exit 1
fi

task_arg="$1"
new_slug_raw="$2"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
tasks_dir="$repo_root/tasks"
index_file="$tasks_dir/INDEX.md"

slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9]/-/g' \
    | sed 's/-\{2,\}/-/g' \
    | sed 's/^-//' \
    | sed 's/-$//'
}

if [[ "$task_arg" = /* ]]; then
  task_dir="$task_arg"
else
  task_dir="$repo_root/$task_arg"
fi
task_dir="$(cd "$(dirname "$task_dir")" && pwd)/$(basename "$task_dir")"

if [[ ! -d "$task_dir" ]]; then
  echo "Task directory does not exist: $task_dir" >&2
  exit 1
fi

task_base="$(basename "$task_dir")"
number_prefix="$(printf '%s' "$task_base" | sed -n 's/^\([0-9][0-9][0-9]\)-.*$/\1/p')"
if [[ -z "$number_prefix" ]]; then
  echo "Task directory does not start with NNN- prefix: $task_base" >&2
  exit 1
fi

new_slug="$(slugify "$new_slug_raw")"
if [[ -z "$new_slug" ]]; then
  echo "New slug is empty after normalization" >&2
  exit 1
fi

new_base="${number_prefix}-${new_slug}"
new_dir="$tasks_dir/$new_base"

if [[ "$task_dir" = "$new_dir" ]]; then
  echo "$new_dir"
  exit 0
fi

if [[ -e "$new_dir" ]]; then
  echo "Destination task directory already exists: $new_dir" >&2
  exit 1
fi

mv "$task_dir" "$new_dir"

if [[ -f "$index_file" ]]; then
  old_link="./$task_base/"
  new_link="./$new_base/"
  python3 - "$index_file" "$old_link" "$new_link" "$task_base" "$new_base" <<'PY'
import sys
from pathlib import Path

index_path = Path(sys.argv[1])
old_link = sys.argv[2]
new_link = sys.argv[3]
old_base = sys.argv[4]
new_base = sys.argv[5]
content = index_path.read_text(encoding="utf-8")
updated = content.replace(old_link, new_link)
updated = updated.replace(f"[{old_base}](", f"[{new_base}](")
index_path.write_text(updated, encoding="utf-8")
PY
fi

echo "$new_dir"
