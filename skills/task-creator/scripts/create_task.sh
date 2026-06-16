#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <title> <summary> [slug] [--project path]..." >&2
  exit 1
fi

title="$1"
summary="$2"
shift 2

custom_slug=""
if [[ $# -gt 0 && "$1" != --* ]]; then
  custom_slug="$1"
  shift
fi

projects=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      if [[ $# -lt 2 ]]; then
        echo "Missing value after --project" >&2
        exit 1
      fi
      projects+=("$2")
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
tasks_dir="$repo_root/tasks"
index_file="$tasks_dir/INDEX.md"

mkdir -p "$tasks_dir"

last_number="$(
  find "$tasks_dir" -mindepth 1 -maxdepth 1 -type d \
    | sed -n 's|.*/\([0-9][0-9][0-9]\)-.*|\1|p' \
    | sort \
    | tail -n 1
)"

if [[ -z "$last_number" ]]; then
  next_number=1
else
  next_number=$((10#$last_number + 1))
fi

printf -v number_padded '%03d' "$next_number"

slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9]/-/g' \
    | sed 's/-\{2,\}/-/g' \
    | sed 's/^-//' \
    | sed 's/-$//'
}

normalize_path() {
  python3 - "$repo_root" "$1" <<'PY'
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
raw = Path(sys.argv[2])
if not raw.is_absolute():
    raw = repo_root / raw
path = raw.resolve()
if not path.exists():
    raise SystemExit(f"Linked path does not exist: {path}")
print(path)
PY
}

markdown_links() {
  local base_dir="$1"
  shift
  if [[ $# -eq 0 ]]; then
    printf '%s\n' "- none"
    return 0
  fi

  local path label rel
  for path in "$@"; do
    rel="$(python3 - "$base_dir" "$path" <<'PY'
import os
import sys

print(os.path.relpath(sys.argv[2], sys.argv[1]))
PY
)"
    label="$(basename "$(dirname "$path")")"
    printf -- '- [%s](%s)\n' "$label" "$rel"
  done
}

table_links() {
  local base_dir="$1"
  shift
  if [[ $# -eq 0 ]]; then
    printf '%s' "-"
    return 0
  fi

  local first=1
  local path rel label
  for path in "$@"; do
    rel="$(python3 - "$base_dir" "$path" <<'PY'
import os
import sys

print(os.path.relpath(sys.argv[2], sys.argv[1]))
PY
)"
    label="$(basename "$(dirname "$path")")"
    if [[ $first -eq 1 ]]; then
      printf '[%s](%s)' "$label" "$rel"
      first=0
    else
      printf '<br>[%s](%s)' "$label" "$rel"
    fi
  done
}

if [[ -n "$custom_slug" ]]; then
  slug="$(slugify "$custom_slug")"
else
  slug="$(slugify "$title")"
fi

if [[ -z "$slug" ]]; then
  slug="task"
fi

task_dir_name="${number_padded}-${slug}"
task_dir="$tasks_dir/$task_dir_name"
task_file="$task_dir/task.md"
plan_file="$task_dir/plan.md"

if [[ -e "$task_dir" ]]; then
  echo "Task directory already exists: $task_dir" >&2
  exit 1
fi

mkdir -p "$task_dir"

normalized_projects=()
if [[ ${#projects[@]} -gt 0 ]]; then
  for path in "${projects[@]}"; do
    normalized_projects+=("$(normalize_path "$path")")
  done
fi

project_links_task="$(markdown_links "$task_dir" ${normalized_projects[@]+"${normalized_projects[@]}"})"
project_links_index="$(table_links "$tasks_dir" ${normalized_projects[@]+"${normalized_projects[@]}"})"

cat > "$task_file" <<EOF
# $title

## Summary
$summary

## Inputs
- none captured yet

## Open Questions
- none

## Status
planned

## Parent Task
none

## Related Tasks
- none

## Projects
$project_links_task
EOF

cat > "$plan_file" <<EOF
# Plan

## Goal
$summary

## Steps
1. Understand the current context.
2. Implement the required change.
3. Verify the result.
EOF

if [[ ! -f "$index_file" ]]; then
  cat > "$index_file" <<'EOF'
# Task Index

| ID | Title | Status | Projects | Directory |
| --- | --- | --- | --- | --- |
EOF
fi

printf '| %s | %s | planned | %s | [%s](./%s/) |\n' \
  "$number_padded" \
  "$title" \
  "$project_links_index" \
  "$task_dir_name" \
  "$task_dir_name" >> "$index_file"

echo "$task_dir"
