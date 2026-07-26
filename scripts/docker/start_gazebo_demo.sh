#!/usr/bin/env bash
set -euo pipefail

WORLD="${PX4_GZ_WORLD:-urban_rescue}"
WORLD_FILE="/app/sim/worlds/${WORLD}.sdf"
if [[ ! -f "$WORLD_FILE" ]]; then
  echo "World file not found: $WORLD_FILE" >&2
  exit 2
fi

export GZ_SIM_RESOURCE_PATH="/app/sim/models:${GZ_SIM_RESOURCE_PATH:-}"
export SIM_ADAPTER="${SIM_ADAPTER:-gazebo_direct}"
export AEROWEAVER_PORT="${AEROWEAVER_PORT:-5001}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"

# Headless Gazebo server. The Web UI is served by AeroWeaver on port 5001.
gz sim --headless-rendering -r -s "$WORLD_FILE" >/tmp/aeroweaver_gazebo.log 2>&1 &
GZ_PID=$!

cleanup() {
  kill "$GZ_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Give Gazebo time to advertise world services before the adapter connects.
sleep "${GAZEBO_STARTUP_DELAY:-8}"
python3 /app/server.py
