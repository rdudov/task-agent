#!/usr/bin/env bash

# Resolve the Python runtime that owns task-agent's installed dependencies.
task_agent_python() {
  local script_dir repo_dir configured local_python
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_dir="$(cd "$script_dir/../../.." && pwd)"
  configured="${TASK_AGENT_PYTHON:-}"
  local_python="$repo_dir/.venv/bin/python"

  if [[ -n "$configured" ]]; then
    if [[ ! -x "$configured" ]]; then
      echo "TASK_AGENT_PYTHON is not executable: $configured" >&2
      return 1
    fi
    printf '%s\n' "$configured"
    return 0
  fi
  if [[ -x "$local_python" ]]; then
    printf '%s\n' "$local_python"
    return 0
  fi
  command -v python3
}
