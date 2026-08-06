#!/usr/bin/env bash
set -euo pipefail

# The one documented writer of the evidence surface, so it has to be able to
# express every outcome its readers distinguish. It used to hardcode
# `- Result: **OK**` whatever it was told, while the owner instruction said in
# the same breath that a gate recorded FAIL refuses the completion -- two
# instructions that could not both be followed. An owner honestly recording a
# failure through the documented path produced a passing gate carrying failure
# prose in its Evidence line.
#
# The result argument is optional and defaults to OK, preserving the outcome
# semantics of existing three-argument callers.

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "Usage: $0 <task-dir> <gate-id> <evidence> [result]" >&2
  echo "  result: OK (default) | PASS | PASSED | FAIL | GAP | BLOCKED" >&2
  exit 1
fi

task_dir="$1"
gate_id="$2"
evidence="$3"
result="${4:-OK}"
result="${result^^}"

# Validated rather than accepted as free text: an unrecognised word would be
# read by the gate as a refusal, which is a confusing way to learn about a typo.
case "$result" in
  OK|PASS|PASSED|FAIL|GAP|BLOCKED) ;;
  *)
    echo "Unknown verification result: $4" >&2
    echo "Use one of: OK, PASS, PASSED (passing) or FAIL, GAP, BLOCKED (refusing)." >&2
    exit 1
    ;;
esac

if [[ ! -d "$task_dir" ]]; then
  echo "Task directory does not exist: $task_dir" >&2
  exit 1
fi

verification_file="$task_dir/verification.md"
date_utc="$(date -u +%F)"

if [[ ! -f "$verification_file" ]]; then
  task_name="$(basename "$task_dir")"
  cat > "$verification_file" <<EOF
# Verification: $task_name

Date: $date_utc
Environment: local, secrets redacted
EOF
fi

cat >> "$verification_file" <<EOF

## $gate_id

- Result: **$result**
- Evidence: $evidence
EOF
