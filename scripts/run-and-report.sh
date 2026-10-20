#!/usr/bin/env bash
# Run a command, capture its exit status before showing any output, and report
# the tail of its combined output. Stdout stays empty so a caller can read the
# status without a pipe masking it.
#
# Usage: scripts/run-and-report.sh <command> [args...]
#
# Environment:
#   RUN_REPORT_LINES  number of trailing output lines to show (default 20)

set -uo pipefail

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 <command> [args...]" >&2
    exit 2
fi

REPORT_LINES="${RUN_REPORT_LINES:-20}"

if ! LOG="$(mktemp "${TMPDIR:-/tmp}/run-and-report.XXXXXX")"; then
    echo "run-and-report: cannot create temporary log" >&2
    exit 3
fi
trap 'rm -f "$LOG"' EXIT

"$@" >"$LOG" 2>&1
status=$?

tail -n "$REPORT_LINES" "$LOG" >&2
echo "EXIT=$status" >&2

exit "$status"
