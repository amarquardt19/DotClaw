#!/bin/bash
# Daily fare snapshot wrapper for launchd — logs to logs/daily_fare_snapshot.log
set -u
ROOT="/Users/adammarq/CascadeProjects/openclaw-inbox-agent"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily fare snapshot run ====="
  cd "$ROOT" && /usr/bin/env python3 scripts/daily_fare_snapshot.py "$@"
  echo "===== exit code $? ====="
} >> "$LOG_DIR/daily_fare_snapshot.log" 2>&1
