#!/usr/bin/env bash
# ============================================================
# AeroWeaver — Start PX4 + Gazebo Simulation
# ============================================================
#
# Usage:
#   ./scripts/start_sim.sh                         # urban_rescue + sensor model
#   ./scripts/start_sim.sh default x500            # control-debug fallback only; not the research showcase
#   ./scripts/start_sim.sh urban_rescue x500       # control-debug fallback only; not the research showcase
#   PX4_DIR=/path/to/PX4-Autopilot ./scripts/start_sim.sh
#
# This starts: DDS Agent + Gazebo + PX4 SITL.
# Then run in another terminal:
#   SIM_ADAPTER=px4 PX4_GZ_WORLD=<world> PX4_SIM_MODEL=<model> python server.py
# ============================================================

set -euo pipefail

# Keep simulation logs bounded. PX4 can emit MB/s when a simulation is misconfigured;
# writing directly to /tmp/*.log without a cap can fill the whole disk.
LOG_DIR="${AEROWEAVER_LOG_DIR:-/tmp}"
AEROWEAVER_LOG_LIMIT_BYTES="${AEROWEAVER_LOG_LIMIT_BYTES:-52428800}"  # 50 MiB per process
bounded_log() {
    local log_file="$1"
    python3 -u -c 'import sys, pathlib; p=pathlib.Path(sys.argv[1]); limit=int(sys.argv[2]); p.parent.mkdir(parents=True, exist_ok=True); f=p.open("wb"); n=0
for chunk in iter(lambda: sys.stdin.buffer.read(65536), b""):
    if n < limit:
        keep = chunk[:max(0, limit-n)]
        f.write(keep); f.flush(); n += len(keep)
' "$log_file" "$AEROWEAVER_LOG_LIMIT_BYTES"
}

AEROWEAVER_KEEP_PX4_LOGS="${AEROWEAVER_KEEP_PX4_LOGS:-0}"
cleanup_px4_ulog_files() {
    # PX4 writes persistent .ulg flight logs into the build/rootfs tree by
    # default. For demos this can grow by hundreds of MB per run and fill a
    # user's disk. Keep them only when explicitly requested.
    if [ "$AEROWEAVER_KEEP_PX4_LOGS" = "1" ]; then
        return
    fi
    if [ -n "${PX4_BUILD:-}" ] && [ -d "$PX4_BUILD" ]; then
        find "$PX4_BUILD" -type f -name '*.ulg' -delete 2>/dev/null || true
    fi
}

configure_px4_logging_policy() {
    local rc_logging="${PX4_BUILD}/etc/init.d/rc.logging"
    local backup="${rc_logging}.aeroweaver.bak"
    [ -f "$rc_logging" ] || return

    if [ "$AEROWEAVER_KEEP_PX4_LOGS" = "1" ]; then
        if [ -f "$backup" ]; then
            cp "$backup" "$rc_logging"
        fi
        echo "PX4 ULog persistence: enabled (AEROWEAVER_KEEP_PX4_LOGS=1)"
        return
    fi

    if [ ! -f "$backup" ]; then
        cp "$rc_logging" "$backup"
    fi
    if ! grep -q "AeroWeaver demo disables persistent PX4 ULog" "$rc_logging"; then
        python3 - "$rc_logging" <<'PYPX4LOG'
from pathlib import Path
import sys
p = Path(sys.argv[1])
text = p.read_text()
insert = """\n# AeroWeaver demo disables persistent PX4 ULog by default.\n# start_sim.sh restores the original file when AEROWEAVER_KEEP_PX4_LOGS=1.\nset LOGGER_ARGS \"\"\nexit 0\n\n"""
if "AeroWeaver demo disables persistent PX4 ULog" not in text:
    lines = text.splitlines(True)
    if lines and lines[0].startswith("#!"):
        text = lines[0] + insert + "".join(lines[1:])
    else:
        text = insert + text
    p.write_text(text)
PYPX4LOG
    fi
    echo "PX4 ULog persistence: disabled for demo (set AEROWEAVER_KEEP_PX4_LOGS=1 to keep .ulg files)"
}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

WORLD="${1:-urban_rescue}"
MODEL="${2:-x500_lidar_2d_cam}"

if [ -d "${PROJECT_DIR}/PX4-Autopilot" ]; then
    PX4_DIR="${PX4_DIR:-${PROJECT_DIR}/PX4-Autopilot}"
elif [ -n "${PX4_DIR:-}" ]; then
    true
else
    echo "ERROR: PX4-Autopilot not found."
    echo "Next: ./scripts/setup_px4.sh"
    exit 1
fi

PX4_BIN="${PX4_DIR}/build/px4_sitl_default/bin/px4"
PX4_BUILD="${PX4_DIR}/build/px4_sitl_default"
PX4_WORLDS="${PX4_DIR}/Tools/simulation/gz/worlds"
PX4_MODELS="${PX4_DIR}/Tools/simulation/gz/models"
LOCAL_MODELS="${HOME}/.simulation-gazebo/models"

if [ ! -f "$PX4_BIN" ]; then
    echo "ERROR: PX4 binary not found at $PX4_BIN"
    echo "Next: ./scripts/setup_px4.sh"
    exit 1
fi

if ! command -v MicroXRCEAgent >/dev/null 2>&1; then
    echo "ERROR: MicroXRCEAgent not found."
    echo "Next: ./scripts/setup_px4.sh"
    exit 1
fi

if ! command -v gz >/dev/null 2>&1; then
    echo "ERROR: Gazebo CLI 'gz' not found."
    echo "macOS: brew tap osrf/simulation && brew install gz-harmonic"
    echo "Ubuntu: install gz-harmonic"
    exit 1
fi

export PX4_GZ_MODELS="$PX4_MODELS"
export PX4_GZ_WORLDS="$PX4_WORLDS"
export GZ_SIM_RESOURCE_PATH="${LOCAL_MODELS}:${PX4_MODELS}:${PX4_WORLDS}:${GZ_SIM_RESOURCE_PATH:-}"
export PX4_SYS_AUTOSTART=4001
export PX4_SIMULATOR=gz
export PX4_GZ_WORLD="$WORLD"
export PX4_SIM_MODEL="$MODEL"
export PX4_GZ_STANDALONE=1

WORLD_SDF="${PX4_WORLDS}/${WORLD}.sdf"
if [ ! -f "$WORLD_SDF" ]; then
    echo "WARNING: World file not found: $WORLD_SDF"
    echo "Available worlds:"
    find "${PX4_WORLDS}" -maxdepth 1 -name '*.sdf' -print 2>/dev/null | xargs -I{} basename {} .sdf || true
    if [ -f "${PX4_WORLDS}/default.sdf" ]; then
        echo "Falling back to world: default"
        WORLD="default"
        WORLD_SDF="${PX4_WORLDS}/default.sdf"
        export PX4_GZ_WORLD="default"
    else
        echo "ERROR: default.sdf is also missing."
        echo "Next: ./scripts/setup_px4.sh"
        exit 1
    fi
fi

if [ ! -d "${LOCAL_MODELS}/${MODEL}" ] && [ ! -d "${PX4_MODELS}/${MODEL}" ]; then
    echo "ERROR: Model '${MODEL}' not found in common Gazebo model directories."
    if [ "$MODEL" = "x500_lidar_2d_cam" ]; then
        echo "The full AeroWeaver research demo requires our modified UAV model."
        echo "Next: ./scripts/setup_px4.sh"
    else
        echo "If you intentionally want a control-debug fallback, ensure the model is installed first."
    fi
    exit 1
fi

if [ "$MODEL" != "x500_lidar_2d_cam" ]; then
    echo "WARNING: You are not using the AeroWeaver modified UAV model."
    echo "  Current model: $MODEL"
    echo "  Research showcase model: x500_lidar_2d_cam"
    echo "  Camera/LiDAR panels may not represent the AeroWeaver demo capability."
fi

cleanup() {
    echo ""
    echo "Shutting down simulation..."
    pkill -f "bin/px4" 2>/dev/null || true
    pkill -f "gz sim" 2>/dev/null || true
    pkill -f MicroXRCEAgent 2>/dev/null || true
    sleep 1
    cleanup_px4_ulog_files
    echo "Done."
}
trap cleanup EXIT INT TERM

echo "============================================================"
echo " AeroWeaver Simulation Launcher"
echo " World: $WORLD | Model: $MODEL"
echo " PX4_DIR: $PX4_DIR"
echo "============================================================"
echo ""

if [ -x "${SCRIPT_DIR}/doctor_gazebo.sh" ]; then
    echo "Preflight doctor (non-live):"
    if ! "${SCRIPT_DIR}/doctor_gazebo.sh" "$WORLD" "$MODEL"; then
        echo ""
        echo "Doctor found blocking issues. Fix them or run ./scripts/setup_px4.sh, then retry."
        exit 1
    fi
    echo ""
fi

cleanup_px4_ulog_files
configure_px4_logging_policy

echo "[1/3] Starting Micro XRCE-DDS Agent..."
MicroXRCEAgent udp4 -p 8888 2>&1 | bounded_log "$LOG_DIR/aeroweaver_dds.log" &
DDS_PID=$!
sleep 1
if kill -0 "$DDS_PID" 2>/dev/null; then
    echo "  DDS Agent running (PID: $DDS_PID)"
else
    echo "ERROR: DDS Agent failed to start. Log: $LOG_DIR/aeroweaver_dds.log"
    tail -80 "$LOG_DIR/aeroweaver_dds.log" 2>/dev/null || true
    exit 1
fi

echo "[2/3] Starting Gazebo ($WORLD)..."
gz sim --verbose=1 -r -s "$WORLD_SDF" 2>&1 | bounded_log "$LOG_DIR/aeroweaver_gz.log" &
GZ_PID=$!
echo "  Waiting for Gazebo to load (10s)..."
sleep 10
if kill -0 "$GZ_PID" 2>/dev/null; then
    echo "  Gazebo running (PID: $GZ_PID)"
else
    echo "ERROR: Gazebo failed to start. Log: $LOG_DIR/aeroweaver_gz.log"
    tail -120 "$LOG_DIR/aeroweaver_gz.log" 2>/dev/null || true
    exit 1
fi

echo "[3/3] Starting PX4 SITL..."
cd "$PX4_BUILD"
"$PX4_BIN" "$PX4_BUILD" -s "${PX4_BUILD}/etc/init.d-posix/rcS" < /dev/null 2>&1 | bounded_log "$LOG_DIR/aeroweaver_px4.log" &
PX4_PID=$!
sleep 8
if kill -0 "$PX4_PID" 2>/dev/null; then
    echo "  PX4 SITL running (PID: $PX4_PID)"
else
    echo "ERROR: PX4 SITL failed to start. Log: $LOG_DIR/aeroweaver_px4.log"
    tail -120 "$LOG_DIR/aeroweaver_px4.log" 2>/dev/null || true
    exit 1
fi

echo ""
echo "============================================================"
echo " Simulation is running!"
echo ""
echo " MAVLink:  udp://:14540 (MAVSDK/Offboard)"
echo "           udp://:14550 (QGroundControl)"
echo ""
echo " In another terminal, run:"
echo "   cd ${PROJECT_DIR}"
echo "   SIM_ADAPTER=px4 PX4_GZ_WORLD=${WORLD} PX4_SIM_MODEL=${MODEL} python server.py"
echo "   curl http://localhost:5001/api/status"
echo "   curl http://localhost:5001/api/sensor/status"
echo ""
echo " Optional live doctor:"
echo "   ./scripts/doctor_gazebo.sh ${WORLD} ${MODEL} --live"
echo ""
echo " Gazebo GUI (optional):"
echo "   gz sim -g"
echo ""
echo " Logs:"
echo "   DDS:     $LOG_DIR/aeroweaver_dds.log"
echo "   Gazebo:  $LOG_DIR/aeroweaver_gz.log"
echo "   PX4:     $LOG_DIR/aeroweaver_px4.log"
if [ "$AEROWEAVER_KEEP_PX4_LOGS" = "1" ]; then
    echo "   PX4 ULog: enabled under $PX4_BUILD/log"
else
    echo "   PX4 ULog: disabled/cleaned for demo safety (set AEROWEAVER_KEEP_PX4_LOGS=1 to keep .ulg)"
fi
echo ""
echo " Press Ctrl+C to stop all."
echo "============================================================"

wait
