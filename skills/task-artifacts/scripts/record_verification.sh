#!/usr/bin/env bash
# Append a verification section to tasks/<id>/verification.md
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: record_verification.sh <task-dir> <section-id> [body text]" >&2
  exit 2
fi

TASK_DIR="$1"
SECTION_ID="$2"
BODY="${3:-}"

if [[ ! -d "$TASK_DIR" ]]; then
  echo "Task directory not found: $TASK_DIR" >&2
  exit 2
fi

FILE="$TASK_DIR/verification.md"
DATE="$(date +%Y-%m-%d)"

if [[ ! -f "$FILE" ]]; then
  TASK_NAME="$(basename "$TASK_DIR")"
  cat >"$FILE" <<EOF
# Verification: $TASK_NAME

Date: $DATE  
Environment: (fill: repo, stand; no secrets)

EOF
fi

{
  echo ""
  echo "## $SECTION_ID"
  echo ""
  echo "- Date: $DATE"
  if [[ -n "$BODY" ]]; then
    echo "- $BODY"
  fi
} >>"$FILE"

echo "Appended section '$SECTION_ID' to $FILE"
