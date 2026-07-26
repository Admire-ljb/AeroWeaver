#!/usr/bin/env bash
# ============================================================
# AeroWeaver — PX4 + Gazebo Simulation Environment Setup
# ============================================================
#
# This script sets up the complete PX4 SITL + Gazebo Harmonic
# simulation environment for AeroWeaver.
#
# Usage:
#   chmod +x scripts/setup_px4.sh
#   ./scripts/setup_px4.sh
#
# What it does:
#   1. Checks prerequisites (CMake, Gazebo, Python)
#   2. Clones PX4-Autopilot (if not present)
#   3. Applies macOS ARM64 build patches
#   4. Downloads PX4 Gazebo base models
#   5. Installs and verifies the bundled AeroWeaver sensor UAV model
#   6. Installs AeroWeaver Gazebo worlds (urban_rescue)
#   7. Creates/reuses a project Python venv and installs PX4 build requirements
#   8. Builds PX4 SITL
#   9. Installs Micro XRCE-DDS Agent (if not present)
#   10. Runs a guided doctor summary
#
# After running this script, use scripts/start_sim.sh to launch.
# ============================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PX4_DIR="${PROJECT_DIR}/PX4-Autopilot"
MODEL_DIR="${HOME}/.simulation-gazebo/models"
WORLD_DIR="${HOME}/.simulation-gazebo/worlds"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ── Step 0: Prerequisites ──────────────────────────────────────

info "Checking prerequisites..."

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        err "$1 not found. $2"
        return 1
    fi
    ok "$1 found: $(command -v "$1")"
}

MISSING=0
check_cmd cmake "Install: brew install cmake (macOS) or apt install cmake (Ubuntu)" || MISSING=1
check_cmd python3 "Install Python 3.10+" || MISSING=1

PYTHON_BIN=""
PIP_BIN=""
BASE_PYTHON=""
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
    if [ -n "${AEROWEAVER_PYTHON:-}" ]; then
        candidates+=("$AEROWEAVER_PYTHON")
    fi
    candidates+=("python3.12" "python3.11" "python3.10" "python3")
    if [ -x "${HOME}/.pyenv/shims/python3" ]; then
        candidates+=("${HOME}/.pyenv/shims/python3")
    fi
    if [ -x "/opt/homebrew/bin/python3" ]; then
        candidates+=("/opt/homebrew/bin/python3")
    fi

    local candidate resolved
    for candidate in "${candidates[@]}"; do
        resolved=""
        if [[ "$candidate" = /* ]] && [ -x "$candidate" ]; then
            resolved="$candidate"
        elif command -v "$candidate" >/dev/null 2>&1; then
            resolved="$(command -v "$candidate")"
        fi
        if [ -n "$resolved" ] && python_is_supported "$resolved"; then
            BASE_PYTHON="$resolved"
            return 0
        fi
    done
    return 1
}

ensure_project_python_env() {
    if ! find_supported_python; then
        err "Python >=3.10 is required for AeroWeaver/PX4 setup. Set AEROWEAVER_PYTHON=/path/to/python3.11 and retry."
        exit 1
    fi
    ok "Base Python selected: $BASE_PYTHON ($(python_version_tuple "$BASE_PYTHON"))"

    local existing_python=""
    if [ -x "${PROJECT_DIR}/venv/bin/python" ]; then
        existing_python="${PROJECT_DIR}/venv/bin/python"
    elif [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
        existing_python="${PROJECT_DIR}/.venv/bin/python"
    fi

    if [ -n "$existing_python" ]; then
        if python_is_supported "$existing_python"; then
            PYTHON_BIN="$existing_python"
        else
            warn "Existing virtualenv uses unsupported Python $(python_version_tuple "$existing_python"); recreating ${PROJECT_DIR}/venv with Python >=3.10"
            rm -rf "${PROJECT_DIR}/venv"
            "$BASE_PYTHON" -m venv "${PROJECT_DIR}/venv"
            PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"
        fi
    else
        info "Creating project Python virtual environment for PX4 build dependencies..."
        "$BASE_PYTHON" -m venv "${PROJECT_DIR}/venv"
        PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"
    fi

    PIP_BIN="$PYTHON_BIN -m pip"
    # Make PX4/CMake discover the same Python environment when they call python/python3.
    export PATH="$(dirname "$PYTHON_BIN"):${PATH}"

    ok "Using Python environment: $PYTHON_BIN ($(python_version_tuple "$PYTHON_BIN"))"
    "$PYTHON_BIN" -m pip install --upgrade pip wheel setuptools
}

install_python_requirements() {
    local requirements_file="$1"
    local label="$2"
    if [ -f "$requirements_file" ]; then
        info "Installing ${label} from ${requirements_file}"
        local install_file="$requirements_file"
        local tmp_requirements=""
        # PX4 v1.15 requirements include legacy specifiers such as matplotlib>=3.0.*,
        # which modern pip/packaging rejects. Sanitize only the temporary copy.
        if grep -Eq ">=[0-9][0-9.]*\.\*" "$requirements_file"; then
            tmp_requirements="$(mktemp)"
            python3 - "$requirements_file" "$tmp_requirements" <<'PYREQ'
import re
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
text = re.sub(r">=([0-9][0-9.]*)\.\*", lambda m: ">=" + m.group(1).rstrip('.'), text)
open(dst, "w", encoding="utf-8").write(text)
PYREQ
            install_file="$tmp_requirements"
            warn "Sanitized legacy wildcard requirements for modern pip"
        fi
        $PIP_BIN install -r "$install_file"
        [ -n "$tmp_requirements" ] && rm -f "$tmp_requirements"
        ok "${label} installed"
    else
        warn "Requirements file not found: $requirements_file"
    fi
}

patch_px4_macos_gz_bridge() {
    local cmake_file="${PX4_DIR}/src/modules/simulation/gz_bridge/CMakeLists.txt"
    if [ ! -f "$cmake_file" ]; then
        warn "PX4 gz_bridge CMake file not found: $cmake_file"
        return
    fi
    if grep -q "AeroWeaver macOS Homebrew protobuf target patch" "$cmake_file"; then
        ok "PX4 gz_bridge protobuf patch already applied"
        return
    fi
    info "Applying PX4 gz_bridge protobuf CMake patch for macOS/Homebrew..."
    python3 - "$cmake_file" <<'PYPATCH'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
needle = "# Find the gz_Transport library\n"
patch = """# AeroWeaver macOS Homebrew protobuf target patch
# Homebrew gz-msgs may export protobuf::libprotobuf in its link interface
# before protobuf has been imported into this CMake project. Import it first.
find_package(protobuf CONFIG QUIET)
if(NOT protobuf_FOUND)
	find_package(Protobuf QUIET)
endif()

"""
if patch.strip() not in text:
    if needle not in text:
        raise SystemExit(f"anchor not found in {path}")
    text = text.replace(needle, patch + needle, 1)
    path.write_text(text, encoding="utf-8")
PYPATCH
    ok "PX4 gz_bridge protobuf patch applied"
}



patch_px4_macos_common_flags() {
    local cmake_file="${PX4_DIR}/cmake/px4_add_common_flags.cmake"
    if [ ! -f "$cmake_file" ]; then
        warn "PX4 common flags file not found: $cmake_file"
        return
    fi
    if grep -q "AeroWeaver macOS Clang warning compatibility patch" "$cmake_file"; then
        ok "PX4 common flags warning compatibility patch already applied"
        return
    fi
    # Upgrade an older AeroWeaver VLA-only patch if present.
    if grep -q "AeroWeaver macOS Clang VLA warning patch" "$cmake_file"; then
        python3 - "$cmake_file" <<'PYUPGRADE'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace("AeroWeaver macOS Clang VLA warning patch", "AeroWeaver macOS Clang warning compatibility patch")
anchor = "\t\t\t-Wno-error=vla-cxx-extension\n"
extra = "\t\t\t-Wno-double-promotion\n\t\t\t-Wno-error=double-promotion\n\t\t\t-Wno-error=attributes\n"
if "-Wno-error=double-promotion" not in text and anchor in text:
    text = text.replace(anchor, anchor + extra, 1)
path.write_text(text, encoding="utf-8")
PYUPGRADE
        ok "PX4 common flags warning compatibility patch upgraded"
        return
    fi
    info "Applying PX4 common flags warning compatibility patch for modern macOS Clang..."
    python3 - "$cmake_file" <<'PYFLAGS'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
needle = """\t\t\t-Wno-varargs\n"""
patch = """\t\t\t# AeroWeaver macOS Clang VLA warning patch\n\t\t\t-Wno-vla\n\t\t\t-Wno-vla-cxx-extension\n\t\t\t-Wno-error=vla\n\t\t\t-Wno-error=vla-cxx-extension\n"""
if patch.strip() not in text:
    if needle not in text:
        raise SystemExit(f"anchor not found in {path}")
    text = text.replace(needle, needle + patch, 1)
    path.write_text(text, encoding="utf-8")
PYFLAGS
    ok "PX4 common flags VLA patch applied"
}

patch_px4_macos_pxh_vla() {
    local source_file="${PX4_DIR}/platforms/posix/src/px4/common/px4_daemon/pxh.cpp"
    if [ ! -f "$source_file" ]; then
        warn "PX4 pxh.cpp not found: $source_file"
        return
    fi
    if grep -q "std::vector<const char \*> arg(words.size() + 1);" "$source_file"; then
        ok "PX4 pxh.cpp VLA patch already applied"
        return
    fi
    info "Applying PX4 pxh.cpp VLA patch for modern macOS Clang..."
    python3 - "$source_file" <<'PYPXH'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = """\t\tconst char *arg[words.size() + 1];\n\n\t\tfor (unsigned i = 0; i < words.size(); ++i) {\n\t\t\targ[i] = (char *)words[i].c_str();\n\t\t}\n\n\t\t// Explicitly set this nullptr.\n\t\targ[words.size()] = nullptr;\n\n\t\tint retval = _apps[command](words.size(), (char **)arg);\n"""
new = """\t\tstd::vector<const char *> arg(words.size() + 1);\n\n\t\tfor (unsigned i = 0; i < words.size(); ++i) {\n\t\t\targ[i] = words[i].c_str();\n\t\t}\n\n\t\t// Explicitly set this nullptr.\n\t\targ[words.size()] = nullptr;\n\n\t\tint retval = _apps[command](words.size(), const_cast<char **>(arg.data()));\n"""
if old not in text:
    raise SystemExit(f"pxh.cpp VLA anchor not found in {path}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
PYPXH
    ok "PX4 pxh.cpp VLA patch applied"
}

# Check Gazebo
if command -v gz &>/dev/null; then
    GZ_VER=$(gz sim --version 2>&1 | head -1)
    ok "Gazebo found: $GZ_VER"
else
    warn "Gazebo (gz) not found."
    echo "  macOS:  brew tap osrf/simulation && brew install gz-harmonic"
    echo "  Ubuntu: sudo apt install gz-harmonic"
    MISSING=1
fi

if [ "$MISSING" = "1" ]; then
    err "Please install missing prerequisites and re-run."
    exit 1
fi

# ── Step 1: Clone PX4 ──────────────────────────────────────────

if [ -d "$PX4_DIR" ]; then
    ok "PX4-Autopilot already exists at $PX4_DIR"
else
    info "Cloning PX4-Autopilot..."
    cd "$PROJECT_DIR"
    git clone https://github.com/PX4/PX4-Autopilot.git --recursive --depth=1 -b v1.15.4
    ok "PX4-Autopilot cloned"
fi

# ── Step 2: macOS ARM64 Patches ────────────────────────────────

ARCH=$(uname -m)
OS=$(uname -s)

if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    info "Applying macOS ARM64 build patches..."
    export CMAKE_POLICY_VERSION_MINIMUM=3.5

    # protobuf fix (brew keg-only)
    if [ -d "/opt/homebrew/Cellar/protobuf@33" ]; then
        PROTO_VER=$(ls /opt/homebrew/Cellar/protobuf@33/ | head -1)
        PROTO_PREFIX="/opt/homebrew/Cellar/protobuf@33/${PROTO_VER}"
        export PKG_CONFIG_PATH="${PROTO_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
        export CMAKE_PREFIX_PATH="${PROTO_PREFIX}:${PROTO_PREFIX}/lib/cmake/protobuf:${CMAKE_PREFIX_PATH:-}"
        export protobuf_DIR="${PROTO_PREFIX}/lib/cmake/protobuf"
        export Protobuf_DIR="${PROTO_PREFIX}/lib/cmake/protobuf"
        ok "protobuf@33 CMake/pkg-config paths set"
    fi

    # VLA and attribute warnings (common on ARM64 clang)
    export CFLAGS="${CFLAGS:-} -Wno-vla -Wno-error=vla"
    export CXXFLAGS="${CXXFLAGS:-} -Wno-vla -Wno-vla-cxx-extension -Wno-error=vla -Wno-error=vla-cxx-extension -Wno-double-promotion -Wno-error=double-promotion -Wno-error=attributes"

    ok "macOS ARM64 patches applied"
fi

# ── Step 3: Download PX4 Gazebo Base Models ────────────────────

info "Setting up Gazebo base models..."
mkdir -p "$MODEL_DIR"

# Download official PX4 models (x500 base, sensors, etc.)
if [ -d "${MODEL_DIR}/x500" ]; then
    ok "PX4 base models already present"
else
    info "Downloading PX4 Gazebo models..."
    MODELS_TMP=$(mktemp -d)
    git clone --depth=1 https://github.com/PX4/PX4-gazebo-models.git "$MODELS_TMP" 2>/dev/null || true
    if [ -d "${MODELS_TMP}/models" ]; then
        cp -r "${MODELS_TMP}/models/"* "$MODEL_DIR/" 2>/dev/null || true
        ok "PX4 Gazebo base models installed to $MODEL_DIR"
    else
        warn "Could not download PX4-gazebo-models. You may need to download manually."
        echo "  git clone https://github.com/PX4/PX4-gazebo-models.git"
        echo "  cp -r PX4-gazebo-models/models/* ~/.simulation-gazebo/models/"
    fi
    rm -rf "$MODELS_TMP"
fi

# ── Step 4: Install AeroWeaver Custom Model ────────────────────

info "Installing and verifying the AeroWeaver modified UAV model..."

AEROWEAVER_MODEL="x500_lidar_2d_cam"
CUSTOM_MODEL_SRC="${PROJECT_DIR}/sim/models/${AEROWEAVER_MODEL}"
CUSTOM_MODEL_DST="${MODEL_DIR}/${AEROWEAVER_MODEL}"
if [ ! -d "$CUSTOM_MODEL_SRC" ]; then
    err "Required AeroWeaver modified UAV model is missing: $CUSTOM_MODEL_SRC"
    echo "  The full research demo requires ${AEROWEAVER_MODEL}; PX4 standard x500 is only a control-debug fallback."
    exit 1
fi

rm -rf "$CUSTOM_MODEL_DST"
cp -r "$CUSTOM_MODEL_SRC" "$MODEL_DIR/"

if [ ! -f "${CUSTOM_MODEL_DST}/model.sdf" ] || [ ! -f "${CUSTOM_MODEL_DST}/model.config" ]; then
    err "AeroWeaver model copy is incomplete: $CUSTOM_MODEL_DST"
    exit 1
fi

for required_sensor in cam_front cam_rear cam_left cam_right cam_down lidar_2d; do
    if ! grep -q "$required_sensor" "${CUSTOM_MODEL_DST}/model.sdf"; then
        err "AeroWeaver model verification failed: missing sensor '${required_sensor}' in model.sdf"
        exit 1
    fi
done

ok "AeroWeaver modified UAV model installed and verified: ${CUSTOM_MODEL_DST}"
echo "  Model: ${AEROWEAVER_MODEL}"
echo "  Sensors: front/rear/left/right/down cameras + 2D LiDAR"

# ── Step 5: Install Custom Gazebo Worlds ───────────────────────

info "Installing AeroWeaver custom Gazebo worlds..."

PX4_WORLDS="${PX4_DIR}/Tools/simulation/gz/worlds"
CUSTOM_WORLDS_SRC="${PROJECT_DIR}/sim/worlds"

if [ -d "$CUSTOM_WORLDS_SRC" ] && [ -d "$PX4_WORLDS" ]; then
    cp "${CUSTOM_WORLDS_SRC}/"*.sdf "$PX4_WORLDS/" 2>/dev/null || true
    ok "Custom worlds installed to $PX4_WORLDS"
    ls "$CUSTOM_WORLDS_SRC/"*.sdf 2>/dev/null | while read f; do
        echo "  - $(basename "$f")"
    done
else
    warn "Custom worlds not found or PX4 worlds dir missing."
fi

# ── Step 6: Python build dependencies ──────────────────────────

ensure_project_python_env
install_python_requirements "${PX4_DIR}/Tools/setup/requirements.txt" "PX4 Python build requirements"
info "Installing PX4 macOS Python build compatibility dependencies (symforce, numpy<2)..."
$PIP_BIN install "numpy<2.0" "symforce>=0.10,<0.11"
ok "PX4 compatibility dependencies installed"

if ! python3 -c "import kconfiglib" >/dev/null 2>&1; then
    err "PX4 Python dependency check failed: kconfiglib is still not importable from $(command -v python3)"
    exit 1
fi
ok "PX4 Python dependency check passed (kconfiglib importable)"
patch_px4_macos_gz_bridge
patch_px4_macos_common_flags
patch_px4_macos_pxh_vla

# ── Step 7: Build PX4 SITL ─────────────────────────────────────

info "Building PX4 SITL (this may take 10-30 minutes on first build)..."
cd "$PX4_DIR"

if [ "$OS" = "Darwin" ]; then
    export CMAKE_POLICY_VERSION_MINIMUM=3.5
fi

# Build the SITL binary only. Do not use `make px4_sitl gz_x500` here:
# that target launches PX4/Gazebo after the build and can leave an interactive
# `pxh>` shell writing huge logs. Runtime launch is handled by start_sim.sh.
if [ -f "build/px4_sitl_default/bin/px4" ]; then
    ok "PX4 SITL binary already exists. Skipping build."
    echo "  To rebuild: cd $PX4_DIR && make px4_sitl_default"
else
    BUILD_LOG="/tmp/aeroweaver_px4_build.log"
    if make px4_sitl_default >"$BUILD_LOG" 2>&1; then
        ok "PX4 SITL build successful!"
    elif [ -f "build/px4_sitl_default/bin/px4" ]; then
        warn "PX4 build command returned non-zero, but the SITL binary exists and will be used. Full log: $BUILD_LOG"
    else
        err "PX4 build failed. Full log: $BUILD_LOG"
        echo "Last 160 build log lines:"
        tail -160 "$BUILD_LOG" 2>/dev/null || true
        echo "Common fixes for macOS ARM64:"
        echo "  export CMAKE_POLICY_VERSION_MINIMUM=3.5"
        echo "  brew install protobuf@33"
        echo "  $PIP_BIN install 'numpy<2.0' 'symforce>=0.10,<0.11'"
        echo "  See README.md for setup and troubleshooting guidance"
        exit 1
    fi
fi

# ── Step 8: Micro XRCE-DDS Agent ───────────────────────────────

if command -v MicroXRCEAgent &>/dev/null; then
    ok "MicroXRCEAgent already installed"
else
    info "MicroXRCEAgent not found. Installing..."
    XRCE_TMP=$(mktemp -d)
    cd "$XRCE_TMP"
    git clone --depth=1 https://github.com/eProsima/Micro-XRCE-DDS-Agent.git
    cd Micro-XRCE-DDS-Agent
    mkdir build && cd build
    cmake .. -DUAGENT_SOCKETCAN_PROFILE=OFF
    make -j$(sysctl -n hw.ncpu 2>/dev/null || nproc)
    sudo make install
    cd "$PROJECT_DIR"
    rm -rf "$XRCE_TMP"
    if command -v MicroXRCEAgent &>/dev/null; then
        ok "MicroXRCEAgent installed"
    else
        warn "MicroXRCEAgent install may have failed. You can try:"
        echo "  See README.md for the simulator setup path"
    fi
fi

# ── Step 9: Runtime Python dependency check ────────────────────

info "Checking Python mavsdk package..."
if python3 -c "import mavsdk" 2>/dev/null; then
    ok "mavsdk Python package installed"
else
    warn "mavsdk is not importable even after dependency installation. Installing it explicitly."
    $PIP_BIN install mavsdk
    ok "mavsdk installed"
fi

# ── Done ────────────────────────────────────────────────────────

echo ""
echo "============================================================"
echo -e "${GREEN} PX4 + Gazebo simulation environment is ready!${NC}"
echo "============================================================"
echo ""
echo "PX4 binary:  ${PX4_DIR}/build/px4_sitl_default/bin/px4"
echo "Models:      ${MODEL_DIR}/"
echo "Worlds:      ${PX4_WORLDS}/"
echo ""
echo "Next steps:"
echo "  1. Check setup:       ./scripts/doctor_gazebo.sh urban_rescue x500_lidar_2d_cam"
echo "  2. Start simulation:  ./scripts/start_sim.sh urban_rescue x500_lidar_2d_cam"
echo "  3. Start AeroWeaver:  SIM_ADAPTER=px4 PX4_GZ_WORLD=urban_rescue PX4_SIM_MODEL=x500_lidar_2d_cam python server.py"
echo "  4. Open browser:      http://localhost:5001"
echo ""
echo "Control-debug fallback only (not the research showcase):"
echo "  ./scripts/start_sim.sh default x500"
echo "  SIM_ADAPTER=px4 PX4_GZ_WORLD=default PX4_SIM_MODEL=x500 python server.py"
echo "  Return to x500_lidar_2d_cam before demos or paper artifact checks."
echo ""
if [ -x "${SCRIPT_DIR}/doctor_gazebo.sh" ]; then
    echo "Doctor summary:"
    "${SCRIPT_DIR}/doctor_gazebo.sh" urban_rescue x500_lidar_2d_cam || true
    echo ""
fi
echo "See README.md for configuration guidance."
