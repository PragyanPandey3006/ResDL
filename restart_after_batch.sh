#!/usr/bin/env bash
# Waits until the bot's log has been idle (no new downloads) for a sustained
# period, then restarts the container with the orphan-removal command.
# Runs detached; safe to leave running after logging out of Kiro.

set -u

PROJECT_DIR="/home/ubuntu/resdl"
LOG_FILE="${PROJECT_DIR}/logs.txt"
STATUS_LOG="${PROJECT_DIR}/restart_watcher.log"

IDLE_THRESHOLD=90     # seconds of no log activity => batch considered finished
POLL=15               # seconds between checks
MAX_WAIT=21600        # hard cap: 6h, then give up waiting and exit without restart

cd "$PROJECT_DIR" || { echo "$(date -u) ERROR: cannot cd to $PROJECT_DIR" >>"$STATUS_LOG"; exit 1; }

echo "$(date -u) watcher started (idle_threshold=${IDLE_THRESHOLD}s, poll=${POLL}s)" >>"$STATUS_LOG"

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT" ]; do
  now=$(date +%s)
  if [ -f "$LOG_FILE" ]; then
    mtime=$(stat -c %Y "$LOG_FILE")
  else
    mtime=$now
  fi
  idle=$(( now - mtime ))
  echo "$(date -u) idle=${idle}s" >>"$STATUS_LOG"
  if [ "$idle" -ge "$IDLE_THRESHOLD" ]; then
    echo "$(date -u) BATCH IDLE CONFIRMED (idle=${idle}s) -> restarting container" >>"$STATUS_LOG"
    echo "$(date -u) last log lines before restart:" >>"$STATUS_LOG"
    tail -n 3 "$LOG_FILE" >>"$STATUS_LOG" 2>&1
    # Detached restart with orphan removal. -d keeps it running after this
    # script exits.
    docker compose up -d --build --remove-orphans >>"$STATUS_LOG" 2>&1
    rc=$?
    echo "$(date -u) docker compose exit code = ${rc}" >>"$STATUS_LOG"
    if [ "$rc" -eq 0 ]; then
      echo "$(date -u) RESTART OK" >>"$STATUS_LOG"
    else
      echo "$(date -u) RESTART FAILED (see output above)" >>"$STATUS_LOG"
    fi
    exit "$rc"
  fi
  sleep "$POLL"
  elapsed=$(( elapsed + POLL ))
done

echo "$(date -u) MAX_WAIT (${MAX_WAIT}s) reached without confirming idle; exiting WITHOUT restart" >>"$STATUS_LOG"
exit 2
