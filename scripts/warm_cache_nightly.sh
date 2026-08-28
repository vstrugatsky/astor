#!/bin/bash
# Nightly warm-cache run + email notification.
#
# Installed via crontab, weekdays at 5pm Pacific:
#   0 17 * * 1-5 /Users/vs/tariel/astor/scripts/warm_cache_nightly.sh
#
# 5pm (not overnight) is deliberate: a failure surfaces with hours to
# fix it, not during the precious first minutes of the next session.
# Weekdays only: Friday's run already warms Monday's session (astor
# resolves "next trading day" itself), so Sat/Sun runs would just be a
# fast no-op re-verification.
#
# Credentials for the notification email live in ~/.astor_notify.env
# (outside the repo, mode 600), never committed -- see astor.core.grading's
# sibling memory notes for context on why this exists at all.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

# cron runs with a minimal environment -- no shell rc files, no activated
# venv, often a bare PATH -- so both the interpreter and the env file are
# referenced by absolute path rather than assumed to be on PATH.
PYTHON="$REPO_DIR/venv/bin/python3"

if [ -f "$HOME/.astor_notify.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$HOME/.astor_notify.env"
    set +a
fi

LOG_FILE="$REPO_DIR/data/warm_cache_nightly.log"
mkdir -p "$(dirname "$LOG_FILE")"

TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
RUN_OUTPUT="$("$PYTHON" -m astor.cli warm-cache --tickers-file universe.txt 2>&1)"
EXIT_CODE=$?

{
    echo "===== warm-cache run: $TS (exit $EXIT_CODE) ====="
    echo "$RUN_OUTPUT"
} >> "$LOG_FILE"

if [ "$EXIT_CODE" -eq 0 ]; then
    SUBJECT="astor warm-cache OK -- $TS"
else
    SUBJECT="astor warm-cache FAILED (exit $EXIT_CODE) -- $TS"
fi

if [ -n "${SMTP_HOST:-}" ]; then
    printf '%s\n' "$RUN_OUTPUT" | "$PYTHON" "$REPO_DIR/scripts/send_email.py" "$SUBJECT" \
        || echo "$(date): failed to send notification email" >> "$LOG_FILE"
else
    echo "$(date): SMTP_HOST not set (missing ~/.astor_notify.env?), skipping notification" >> "$LOG_FILE"
fi
