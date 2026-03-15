#!/usr/bin/env bash
# Start Condora service with nohup.  Kills any existing uvicorn first.
set -euo pipefail
cd "$(dirname "$0")/.."

# Clear stale bytecode
find src -name "*.pyc" -delete

# Kill old uvicorn
pkill -9 -f "uvicorn condora" 2>/dev/null && sleep 2 || true

# Activate venv
source .venv/bin/activate

# Start with nohup — logs to condora.log
nohup env \
  CONDORA_CONDOR_HOST="${CONDORA_CONDOR_HOST:-localhost:9618}" \
  CONDORA_LIFECYCLE_CYCLE_INTERVAL="${CONDORA_LIFECYCLE_CYCLE_INTERVAL:-30}" \
  CONDORA_EXTRA_COLLECTORS="${CONDORA_EXTRA_COLLECTORS:-}" \
  CONDORA_SPOOL_MOUNT="${CONDORA_SPOOL_MOUNT:-}" \
  CONDORA_REMOTE_SPOOL_PREFIX="${CONDORA_REMOTE_SPOOL_PREFIX:-}" \
  CONDORA_REMOTE_SCHEDD="${CONDORA_REMOTE_SCHEDD:-}" \
  CONDORA_SEC_TOKEN_DIRECTORY="${CONDORA_SEC_TOKEN_DIRECTORY:-}" \
  uvicorn condora.main:create_app --factory --host 0.0.0.0 --port 8080 \
  >> condora.log 2>&1 &

echo "Condora started (PID $!) — logging to condora.log"
