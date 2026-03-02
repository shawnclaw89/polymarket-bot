#!/bin/bash
# watchdog.sh — Auto-restart kalshi-bot if it dies
# Usage: nohup bash watchdog.sh >> logs/watchdog.log 2>&1 &

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$BOT_DIR/logs/watchdog.log"
RESTART_DELAY=30  # seconds to wait before restarting

mkdir -p "$BOT_DIR/logs"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG"; }

# Prevent duplicate watchdog/bot instances
LOCKFILE="$BOT_DIR/logs/watchdog.lock"
if [ -f "$LOCKFILE" ] && kill -0 "$(cat "$LOCKFILE")" 2>/dev/null; then
    log "Another watchdog is already running (PID $(cat "$LOCKFILE")) — exiting."
    exit 1
fi
echo $$ > "$LOCKFILE"
trap "rm -f '$LOCKFILE'; exit" INT TERM EXIT

log "Watchdog started (PID $$)"

while true; do
    # Kill any stray bot.py before starting a fresh one
    existing=$(pgrep -f "python3 bot.py" 2>/dev/null | grep -v $$)
    if [ -n "$existing" ]; then
        log "Killing stray bot process(es): $existing"
        kill $existing 2>/dev/null
        sleep 2
    fi

    log "Starting bot..."
    cd "$BOT_DIR"
    python3 bot.py >> "$BOT_DIR/logs/bot.log" 2>&1
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        log "Bot exited cleanly (code 0) — stopping watchdog."
        break
    fi

    log "Bot crashed (code $EXIT_CODE) — restarting in ${RESTART_DELAY}s..."
    sleep $RESTART_DELAY
done
