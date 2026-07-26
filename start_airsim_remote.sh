#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
fi

export SIM_ADAPTER="${SIM_ADAPTER:-airsim}"
export AIRSIM_HOST="${AIRSIM_HOST:-127.0.0.1}"
export AIRSIM_PORT="${AIRSIM_PORT:-41451}"
export AEROWEAVER_PORT="${AEROWEAVER_PORT:-5001}"
export AIRSIM_CAMERA_RELAY_ENABLED="${AIRSIM_CAMERA_RELAY_ENABLED:-false}"
export AIRSIM_CAMERA_RELAY_URL="${AIRSIM_CAMERA_RELAY_URL:-http://127.0.0.1:8765}"

exec "${PYTHON_BIN}" server.py
