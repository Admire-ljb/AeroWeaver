#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
PID_FILE="${AEROWEAVER_PID_FILE:-/tmp/aeroweaver-server.pid}"
LOG_DIR="${AEROWEAVER_LOG_DIR:-${PROJECT_DIR}/logs}"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}")"
  if kill -0 "${old_pid}" 2>/dev/null; then
    kill "${old_pid}"
    for _ in {1..20}; do
      kill -0 "${old_pid}" 2>/dev/null || break
      sleep 0.25
    done
  fi
fi

mkdir -p "${LOG_DIR}"
nohup "${SCRIPT_DIR}/start_airsim_remote.sh" \
  > "${LOG_DIR}/server.log" 2>&1 < /dev/null &
echo "$!" > "${PID_FILE}"

for _ in {1..40}; do
  if curl -fsS --max-time 2 \
      "http://127.0.0.1:${AEROWEAVER_PORT:-5001}/api/status" >/dev/null; then
    echo "AeroWeaver restarted with PID $(cat "${PID_FILE}")"
    exit 0
  fi
  sleep 0.5
done

tail -n 120 "${LOG_DIR}/server.log"
exit 1
