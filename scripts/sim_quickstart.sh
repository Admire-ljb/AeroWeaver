#!/usr/bin/env bash
# ============================================================
# AeroWeaver — Full PX4/Gazebo Research Demo Quickstart
# ============================================================
# Starts the full local research demo stack:
#   1) optional PX4/Gazebo setup
#   2) PX4 + Gazebo + Micro XRCE-DDS Agent
#   3) AeroWeaver backend with PX4 adapter
#   4) UI/API/sensor health checks
#
# Usage:
#   ./scripts/sim_quickstart.sh --setup          # first run: install/build prerequisites managed by this repo
#   ./scripts/sim_quickstart.sh                  # next runs: start simulator + backend
#   ./scripts/sim_quickstart.sh --restart        # stop previous repo-started stack, then start again
#   ./scripts/sim_quickstart.sh default x500     # control-debug fallback only; not the research showcase
#
# This script is intentionally conservative: it only kills processes that were
# started through its own pid files unless --restart is given.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SETUP=0
RESTART=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --setup) SETUP=1 ;;
    --restart) RESTART=1 ;;
    -h|--help)
      sed -n '1,36p' "$0"
      exit 0
      ;;
    *) ARGS+=("$arg") ;;
  esac
done

WORLD="${PX4_GZ_WORLD:-${ARGS[0]:-urban_rescue}}"
MODEL="${PX4_SIM_MODEL:-${ARGS[1]:-x500_lidar_2d_cam}}"
HOST="${AEROWEAVER_HOST:-127.0.0.1}"
PORT="${AEROWEAVER_PORT:-5001}"
BASE_URL="http://${HOST}:${PORT}"
GZ_PYTHONPATH="${GZ_PYTHONPATH:-}"
START_GAZEBO_GUI="${START_GAZEBO_GUI:-1}"

SIM_PID_FILE="/tmp/aeroweaver_full_sim.pid"
SERVER_PID_FILE="/tmp/aeroweaver_full_server.pid"
GUI_PID_FILE="/tmp/aeroweaver_gz_gui.pid"
SIM_LOG="/tmp/aeroweaver_full_sim_launcher.log"
SERVER_LOG="/tmp/aeroweaver_full_server.log"
GUI_LOG="/tmp/aeroweaver_gz_gui.log"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; RED=$'\033[0;31m'; NC=$'\033[0m'
else
  GREEN=''; YELLOW=''; BLUE=''; RED=''; NC=''
fi

info() { printf "%b[INFO]%b %s\n" "$BLUE" "$NC" "$1"; }
ok() { printf "%b[OK]%b %s\n" "$GREEN" "$NC" "$1"; }
warn() { printf "%b[WARN]%b %s\n" "$YELLOW" "$NC" "$1"; }
err() { printf "%b[ERROR]%b %s\n" "$RED" "$NC" "$1"; }

python_version_tuple() {
  "$1" - <<'PYVER' 2>/dev/null
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PYVER
}

python_is_supported() {
  "$1" - <<'PYVER' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PYVER
}

find_supported_python() {
  local candidates=()
  if [ -n "${AEROWEAVER_PYTHON:-}" ]; then candidates+=("$AEROWEAVER_PYTHON"); fi
  candidates+=("python3.12" "python3.11" "python3.10" "python3")
  [ -x "${HOME}/.pyenv/shims/python3" ] && candidates+=("${HOME}/.pyenv/shims/python3")
  [ -x "/opt/homebrew/bin/python3" ] && candidates+=("/opt/homebrew/bin/python3")
  local candidate resolved
  for candidate in "${candidates[@]}"; do
    resolved=""
    if [[ "$candidate" = /* ]] && [ -x "$candidate" ]; then resolved="$candidate"; elif command -v "$candidate" >/dev/null 2>&1; then resolved="$(command -v "$candidate")"; fi
    if [ -n "$resolved" ] && python_is_supported "$resolved"; then printf "%s" "$resolved"; return 0; fi
  done
  return 1
}

is_pid_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

stop_from_pid_file() {
  local file="$1"
  local label="$2"
  if [ -f "$file" ]; then
    local pid
    pid="$(cat "$file" 2>/dev/null || true)"
    if is_pid_alive "$pid"; then
      info "Stopping previous ${label} process (PID ${pid})"
      local pgid current_pgid
      pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
      current_pgid="$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ' || true)"
      kill "$pid" >/dev/null 2>&1 || true
      # Processes started by start_sim.sh often share a process group. Kill that
      # group only when it is not the current quickstart shell's group; otherwise
      # `--restart` can kill itself in terminal/agent wrappers. Killing start_sim
      # directly is normally enough because its EXIT trap cleans DDS/Gazebo/PX4.
      if [ -n "$pgid" ] && [ "$pgid" != "$current_pgid" ]; then
        kill -TERM "-$pgid" >/dev/null 2>&1 || true
      fi
      sleep 2
      if is_pid_alive "$pid"; then
        warn "${label} did not stop gracefully; sending SIGKILL"
        kill -KILL "$pid" >/dev/null 2>&1 || true
        if [ -n "$pgid" ] && [ "$pgid" != "$current_pgid" ]; then
          kill -KILL "-$pgid" >/dev/null 2>&1 || true
        fi
        sleep 1
      fi
    fi
    rm -f "$file"
  fi
}

wait_http() {
  local url="$1"
  local name="$2"
  local max_wait="${3:-90}"
  for i in $(seq 1 "$max_wait"); do
    if curl -fsS "$url" >/tmp/aeroweaver_quickstart_http.json 2>/dev/null; then
      ok "$name is ready: $url"
      return 0
    fi
    sleep 1
  done
  err "$name did not become ready: $url"
  return 1
}

wait_mavsdk_control() {
  local max_wait="${1:-90}"
  info "Waiting for PX4 MAVSDK control link (udp://:14540)"
  for i in $(seq 1 "$max_wait"); do
    if "$PYTHON" - <<'PYMAV' >/tmp/aeroweaver_quickstart_mavsdk.log 2>&1
import asyncio, time
from mavsdk import System
async def main():
    drone = System()
    await drone.connect(system_address="udp://:14540")
    deadline = time.time() + 4
    async for state in drone.core.connection_state():
        if state.is_connected:
            return 0
        if time.time() > deadline:
            return 1
    return 1
raise SystemExit(asyncio.run(main()))
PYMAV
    then
      ok "PX4 MAVSDK control link is ready"
      return 0
    fi
    sleep 1
  done
  err "PX4 MAVSDK control link did not become ready. Last MAVSDK probe log:"
  tail -80 /tmp/aeroweaver_quickstart_mavsdk.log 2>/dev/null || true
  return 1
}

wait_control_adapter() {
  local max_wait="${1:-90}"
  info "Checking PX4 control adapter (must not be mock)"
  for i in $(seq 1 "$max_wait"); do
    if curl -fsS "$BASE_URL/api/adapter/status" >/tmp/aeroweaver_quickstart_adapter.json 2>/dev/null; then
      if "$PYTHON" - <<'PYADAPTER'
import json
from pathlib import Path
data = json.loads(Path('/tmp/aeroweaver_quickstart_adapter.json').read_text())
raise SystemExit(0 if data.get('adapter') == 'px4' and data.get('connected') else 1)
PYADAPTER
      then
        ok "PX4 control adapter connected"
        return 0
      fi
    fi
    sleep 1
  done
  err "PX4 control adapter is not connected. Current adapter status:"
  cat /tmp/aeroweaver_quickstart_adapter.json 2>/dev/null || true
  echo ""
  tail -160 "$SERVER_LOG" 2>/dev/null || true
  return 1
}

ensure_python_env() {
  local base_python
  base_python="$(find_supported_python || true)"
  if [ -z "$base_python" ]; then
    err "Python >=3.10 is required. Set AEROWEAVER_PYTHON=/path/to/python3.11 and retry."
    exit 1
  fi

  local existing_python=""
  if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
    existing_python="$PROJECT_DIR/venv/bin/python"
  elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    existing_python="$PROJECT_DIR/.venv/bin/python"
  fi

  if [ -n "$existing_python" ]; then
    if python_is_supported "$existing_python"; then
      PYTHON="$existing_python"
      ok "Using existing Python environment: $PYTHON ($(python_version_tuple "$PYTHON"))"
      return
    fi
    if [ "$SETUP" = "1" ]; then
      warn "Existing virtualenv uses unsupported Python $(python_version_tuple "$existing_python"); recreating venv with $base_python"
      rm -rf "$PROJECT_DIR/venv"
    else
      err "Existing virtualenv uses unsupported Python $(python_version_tuple "$existing_python"). Run ./scripts/sim_quickstart.sh --setup to recreate it."
      exit 1
    fi
  fi

  if [ "$SETUP" = "1" ]; then
    info "Creating Python virtual environment with $base_python ($(python_version_tuple "$base_python"))"
    "$base_python" -m venv "$PROJECT_DIR/venv"
    PYTHON="$PROJECT_DIR/venv/bin/python"
    "$PYTHON" -m pip install --upgrade pip wheel setuptools
    "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements/base.txt"
    ok "Python dependencies installed"
    return
  fi

  PYTHON="$base_python"
  warn "No venv/.venv found; using Python: $PYTHON ($(python_version_tuple "$PYTHON"))"
}

ensure_app_python_deps() {
  if "$PYTHON" -c "import flask, flask_socketio, flask_cors, mavsdk" >/dev/null 2>&1; then
    ok "Backend Python dependencies are importable"
    return
  fi
  if [ ! -f "$PROJECT_DIR/requirements/base.txt" ]; then
    err "requirements/base.txt not found; cannot install backend Python dependencies."
    exit 1
  fi
  info "Installing backend Python dependencies"
  "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements/base.txt"
  ok "Backend Python dependencies installed"
}

ensure_gazebo_python_path() {
  if "$PYTHON" -c "import gz.transport13, gz.msgs10.image_pb2" >/dev/null 2>&1; then
    ok "Gazebo Python bindings are importable"
    return
  fi

  local pyver transport_site msgs_site math_site msgs_legacy_site combo
  pyver="$($PYTHON - <<'PYVER'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PYVER
)"

  # Use only Gazebo Cellar paths. Do not add /opt/homebrew/lib/pythonX/site-packages,
  # because that can shadow the venv with unrelated Homebrew packages.
  local transport_candidates=("/opt/homebrew/Cellar/gz-transport13"/*"/lib/python${pyver}/site-packages")
  local msgs_candidates=("/opt/homebrew/Cellar/gz-msgs10"/*"/lib/python${pyver}/site-packages")
  local math_candidates=("/opt/homebrew/Cellar/gz-math7"/*"/lib/python${pyver}/site-packages")
  local msgs_legacy_candidates=("/opt/homebrew/Cellar/gz-msgs10"/*"/lib/python")

  for transport_site in "${transport_candidates[@]}"; do
    [ -d "$transport_site/gz" ] || continue
    for msgs_site in "${msgs_candidates[@]}"; do
      [ -d "$msgs_site/gz" ] || continue
      for math_site in "${math_candidates[@]}"; do
        [ -d "$math_site/gz" ] || math_site=""
        for msgs_legacy_site in "${msgs_legacy_candidates[@]}"; do
          [ -d "$msgs_legacy_site/gz" ] || msgs_legacy_site=""
          combo="$transport_site:$msgs_site${math_site:+:$math_site}${msgs_legacy_site:+:$msgs_legacy_site}"
          if PYTHONPATH="$combo:${PYTHONPATH:-}" "$PYTHON" -c "import gz.transport13, gz.msgs10.image_pb2" >/dev/null 2>&1; then
            GZ_PYTHONPATH="$combo${GZ_PYTHONPATH:+:$GZ_PYTHONPATH}"
            export PYTHONPATH="$combo:${PYTHONPATH:-}"
            ok "Gazebo Python bindings found: $combo"
            return
          fi
        done
      done
    done
  done

  warn "Gazebo Python bindings are not importable from the current Python environment; camera panels may show NO SIGNAL."
}

ensure_frontend_build() {
  if [ -f "$PROJECT_DIR/frontend/dist/index.html" ]; then
    ok "Frontend build exists: frontend/dist/index.html"
    return
  fi
  if ! command -v npm >/dev/null 2>&1; then
    warn "npm not found; backend can still run, but build the frontend later with: cd frontend && npm install && npm run build"
    return
  fi
  info "Building Web UI"
  (cd "$PROJECT_DIR/frontend" && npm install --no-audit --no-fund && npm run build)
  ok "Frontend built"
}

printf "============================================================\n"
printf " AeroWeaver full simulator quickstart\n"
printf " Project: %s\n" "$PROJECT_DIR"
printf " World:   %s\n" "$WORLD"
printf " Model:   %s\n" "$MODEL"
printf " UI:      %s\n" "$BASE_URL"
printf "============================================================\n\n"

cd "$PROJECT_DIR"

if [ "$RESTART" = "1" ]; then
  stop_from_pid_file "$SERVER_PID_FILE" "AeroWeaver backend"
  stop_from_pid_file "$GUI_PID_FILE" "Gazebo GUI"
  stop_from_pid_file "$SIM_PID_FILE" "simulation launcher"
fi

ensure_python_env
ensure_app_python_deps
ensure_gazebo_python_path
ensure_frontend_build

if [ "$SETUP" = "1" ]; then
  info "Running first-time PX4/Gazebo setup"
  "$SCRIPT_DIR/setup_px4.sh"
fi

info "Running preflight doctor"
if ! "$SCRIPT_DIR/doctor_gazebo.sh" "$WORLD" "$MODEL"; then
  err "Preflight doctor found blocking issues."
  echo ""
  if [ ! -x "$PROJECT_DIR/PX4-Autopilot/build/px4_sitl_default/bin/px4" ]; then
    echo "PX4 SITL binary is missing. Run the setup path once; it now installs PX4 Python build dependencies automatically:"
  else
    echo "First-time setup command:"
  fi
  echo "  ./scripts/sim_quickstart.sh --setup"
  echo ""
  echo "Control-debug fallback only, not the research showcase:"
  echo "  ./scripts/sim_quickstart.sh default x500"
  exit 1
fi

if [ -f "$SIM_PID_FILE" ] && is_pid_alive "$(cat "$SIM_PID_FILE")"; then
  warn "Simulation launcher already running (PID $(cat "$SIM_PID_FILE")). Use --restart to restart it."
else
  info "Starting PX4 + Gazebo simulation launcher"
  WORLD="$WORLD" MODEL="$MODEL" "$SCRIPT_DIR/start_sim.sh" "$WORLD" "$MODEL" >"$SIM_LOG" 2>&1 &
  echo $! > "$SIM_PID_FILE"
  ok "Simulation launcher started (PID $(cat "$SIM_PID_FILE"), log: $SIM_LOG)"
fi

info "Waiting for simulator startup"
sleep 18
if ! is_pid_alive "$(cat "$SIM_PID_FILE" 2>/dev/null || true)"; then
  err "Simulation launcher exited early. Last log lines:"
  tail -120 "$SIM_LOG" 2>/dev/null || true
  exit 1
fi
wait_mavsdk_control 90 || {
  tail -160 "$SIM_LOG" 2>/dev/null || true
  exit 1
}

if [ -f "$SERVER_PID_FILE" ] && is_pid_alive "$(cat "$SERVER_PID_FILE")"; then
  warn "AeroWeaver backend already running (PID $(cat "$SERVER_PID_FILE")). Use --restart to restart it."
else
  info "Starting AeroWeaver backend with PX4 adapter"
  PYTHONPATH="${GZ_PYTHONPATH:+$GZ_PYTHONPATH:}${PYTHONPATH:-}" \
  SIM_ADAPTER=px4 PX4_GZ_WORLD="$WORLD" PX4_SIM_MODEL="$MODEL" AEROWEAVER_PORT="$PORT" \
    "$PYTHON" backend/server.py >"$SERVER_LOG" 2>&1 &
  echo $! > "$SERVER_PID_FILE"
  ok "Backend started (PID $(cat "$SERVER_PID_FILE"), log: $SERVER_LOG)"
fi

wait_http "$BASE_URL/api/status" "AeroWeaver backend" 90 || {
  tail -120 "$SERVER_LOG" 2>/dev/null || true
  exit 1
}

info "Initializing AeroWeaver runtime"
if curl -fsS -X POST "$BASE_URL/api/init" >/tmp/aeroweaver_quickstart_init.json 2>/dev/null; then
  ok "Runtime initialized"
else
  warn "Runtime init endpoint did not return success yet. You can still click Initialize System in the UI."
fi
wait_control_adapter 90 || exit 1

if [ "$START_GAZEBO_GUI" = "1" ]; then
  if [ -f "$GUI_PID_FILE" ] && is_pid_alive "$(cat "$GUI_PID_FILE")"; then
    ok "Gazebo GUI already running (PID $(cat "$GUI_PID_FILE"))"
  elif command -v gz >/dev/null 2>&1; then
    info "Starting Gazebo GUI (set START_GAZEBO_GUI=0 for headless)"
    gz sim -g >"$GUI_LOG" 2>&1 &
    echo $! > "$GUI_PID_FILE"
    ok "Gazebo GUI started (PID $(cat "$GUI_PID_FILE"), log: $GUI_LOG)"
  else
    warn "gz CLI not found; skipping Gazebo GUI"
  fi
fi

info "Checking sensor bridge"
SENSOR_READY=0
for i in $(seq 1 45); do
  if curl -fsS "$BASE_URL/api/sensor/status" >/tmp/aeroweaver_quickstart_sensor.json 2>/dev/null; then
    if "$PYTHON" - <<'PYSENSOR'
import json
from pathlib import Path
try:
    data = json.loads(Path('/tmp/aeroweaver_quickstart_sensor.json').read_text())
    raise SystemExit(0 if data.get('running') else 1)
except Exception:
    raise SystemExit(1)
PYSENSOR
    then
      SENSOR_READY=1
      break
    fi
  fi
  sleep 1
done
if [ "$SENSOR_READY" = "1" ]; then
  ok "Sensor bridge is running"
  cat /tmp/aeroweaver_quickstart_sensor.json
  echo ""
else
  warn "Sensor bridge endpoint is reachable but not running yet. Latest status:"
  cat /tmp/aeroweaver_quickstart_sensor.json 2>/dev/null || true
  echo ""
  warn "Run live diagnostics: ./scripts/doctor_gazebo.sh ${WORLD} ${MODEL} --live"
fi

info "Checking camera JPEG endpoint"
CAMERA_READY=0
if curl -fsS "$BASE_URL/api/sensor/camera" -o /tmp/aeroweaver_quickstart_camera.jpg 2>/dev/null; then
  if "$PYTHON" - <<'PYCAMERA'
from pathlib import Path
p = Path('/tmp/aeroweaver_quickstart_camera.jpg')
data = p.read_bytes() if p.exists() else b''
raise SystemExit(0 if data.startswith(b'\xff\xd8') and len(data) > 1000 else 1)
PYCAMERA
  then
    CAMERA_READY=1
  fi
fi
if [ "$CAMERA_READY" = "1" ]; then
  ok "Camera endpoint returns JPEG: /tmp/aeroweaver_quickstart_camera.jpg"
else
  warn "Camera endpoint is not returning JPEG yet. Check $SERVER_LOG and ensure Gazebo Python bindings are on PYTHONPATH."
fi

printf "\n============================================================\n"
printf "%bFull simulator stack is running.%b\n" "$GREEN" "$NC"
printf "\n"
printf "Open Web UI:\n  %s\n\n" "$BASE_URL"
printf "Control check:\n  curl %s/api/adapter/status  # must show adapter=px4 and connected=true\n\n" "$BASE_URL"
printf "Camera check:\n  Open Cockpit / camera panels. If they show NO SIGNAL, run:\n"
printf "  ./scripts/doctor_gazebo.sh %s %s --live\n\n" "$WORLD" "$MODEL"
printf "LLM setup for autonomous flight:\n"
printf "  1) cp .env.example .env\n"
printf "  2) edit ACTIVE_PROVIDER / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL\n"
printf "  3) restart with: ./scripts/sim_quickstart.sh --restart\n"
printf "  4) or configure providers in the Web UI Model Configuration panel\n\n"
printf "Try after LLM is configured:\n"
printf "  Initialize System → AI mode → 'Take off to 15 meters and observe the surroundings.'\n\n"
printf "Logs:\n"
printf "  Simulator launcher: %s\n" "$SIM_LOG"
printf "  Backend:            %s\n" "$SERVER_LOG"
printf "  DDS/Gazebo/PX4:     /tmp/aeroweaver_dds.log /tmp/aeroweaver_gz.log /tmp/aeroweaver_px4.log\n"
printf "  Gazebo GUI:         %s\n" "$GUI_LOG"
printf "  Camera snapshot:    /tmp/aeroweaver_quickstart_camera.jpg\n\n"
printf "Stop/restart this stack:\n"
printf "  ./scripts/sim_quickstart.sh --restart\n"
printf "  kill \$(cat %s) \$(cat %s) \$(cat %s) 2>/dev/null || true\n" "$SERVER_PID_FILE" "$GUI_PID_FILE" "$SIM_PID_FILE"
printf "============================================================\n"
