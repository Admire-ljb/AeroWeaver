"""
server.py — AeroWeaver 控制台后端服务

提供：
  - REST API  : 系统初始化、世界状态查询、技能列表
  - WebSocket : 实时技能执行、执行日志推送、状态更新
  - 模式切换  : 手动模式（用户按按钮选技能）↔ AI 模式（LLM 自主规划执行）

启动：
  pip install flask flask-socketio flask-cors
  python backend/server.py

前端连接：
  ws://localhost:5001  (Socket.IO)
  http://localhost:5001/api/...
"""

import sys
import os
import json
import time
import base64
import threading
import secrets
# Phase 0 refactor: removed doctor, device_manager, bootstrap modules
import logging
import requests
from dataclasses import dataclass
from typing import Optional

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)

from flask import Flask, Response, jsonify, request, send_from_directory, send_file, stream_with_context
from flask_socketio import SocketIO, emit
from flask_cors import CORS

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── 静态文件目录（React build 产物）────────────────────────────────────────────
_BASE_DIR = _BACKEND_DIR
_UI_DIST = os.path.join(_PROJECT_ROOT, "frontend", "dist")
_FLEET_STATE_PATH = os.path.join(_PROJECT_ROOT, ".aeroweaver_fleet.json")
_AIRSIM_POOL_SIZE = max(1, min(int(os.getenv("AEROWEAVER_AIRSIM_POOL_SIZE", "10")), 12))


def _load_persisted_fleet_count() -> int:
    default = int(os.getenv("AEROWEAVER_UAV_COUNT", "4"))
    try:
        with open(_FLEET_STATE_PATH, "r", encoding="utf-8") as handle:
            default = int((json.load(handle) or {}).get("active_count", default))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return max(1, min(default, _AIRSIM_POOL_SIZE))


def _persist_fleet_count(count: int) -> None:
    payload = {
        "active_count": max(1, min(int(count), _AIRSIM_POOL_SIZE)),
        "pool_size": _AIRSIM_POOL_SIZE,
        "updated_at": time.time(),
    }
    temp_path = f"{_FLEET_STATE_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, _FLEET_STATE_PATH)

# ── Flask 应用 ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=_UI_DIST, static_url_path="")
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "aeroweaver-dev")
CORS(app, resources={r"/api/*": {"origins": "*"}, r"/socket.io/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    allow_unsafe_werkzeug=True)

AIRSIM_CAMERA_RELAY_URL = os.getenv(
    "AIRSIM_CAMERA_RELAY_URL",
    "http://127.0.0.1:8765",
).rstrip("/")
AIRSIM_CAMERA_RELAY_ENABLED = (
    os.getenv("AIRSIM_CAMERA_RELAY_ENABLED", "true").strip().lower()
    in {"1", "true", "yes", "on"}
    and bool(AIRSIM_CAMERA_RELAY_URL)
)
AIRSIM_RELAY_CHANNELS = frozenset(
    {"scene", "front", "rear", "left", "right", "down"}
)

# ══════════════════════════════════════════════════════════════════════════════
#  全局状态
# ══════════════════════════════════════════════════════════════════════════════

class AppState:
    """应用全局状态，单例。"""

    def __init__(self):
        self.mode: str = "manual"          # "manual" | "ai"
        self.is_executing: bool = False    # AI/plan-level execution state
        self.executing_robots: set[str] = set()
        self._execution_lock = threading.Lock()
        self.current_robot: str = "UAV_1"  # 当前选中的机器人
        self._desired_airsim_fleet_count: int = _load_persisted_fleet_count()

        # 核心模块（延迟初始化）
        # robot_registries: {robot_id → SkillRegistry}  每台机器人独立注册表，技能执行历史互不干扰
        self.robot_registries: dict = {}
        self.world_model = None
        self.episodic_memory = None
        self.skill_memory = None
        self.runtime = None
        self.initialized: bool = False
        self.experience_store = None  # VectorStore 单例，用于经验检索

        # 传感器桥接
        self.sensor_bridge = None

        # AI 模式线程
        self._ai_thread: Optional[threading.Thread] = None
        self._ai_stop_event = threading.Event()
        self._current_agent_loop = None  # 当前运行的 AgentLoop 实例

        # 通用设备协议状态（docs/DEVICE_PROTOCOL.md）
        # devices: device_id -> registered metadata/runtime state
        self.devices: dict = {}
        self.device_tokens: dict = {}
        self.device_sids: dict = {}
        self._device_lock = threading.Lock()

        # 执行日志缓冲（最多保留 200 条）
        self.log_buffer: list[dict] = []
        self._log_lock = threading.Lock()

    def try_begin_robot_execution(self, robot_id: str) -> bool:
        """Reserve one robot while allowing other robots to run concurrently."""
        reserved, _ = self.try_begin_robot_executions([robot_id])
        return reserved

    def try_begin_robot_executions(self, robot_ids) -> tuple[bool, list[str]]:
        """Atomically reserve all robots needed by a single or swarm skill."""
        requested = list(dict.fromkeys(str(item) for item in robot_ids if str(item)))
        with self._execution_lock:
            busy = sorted(set(requested) & self.executing_robots)
            if busy:
                return False, busy
            self.executing_robots.update(requested)
            return True, []

    def end_robot_execution(self, robot_id: str) -> None:
        self.end_robot_executions([robot_id])

    def end_robot_executions(self, robot_ids) -> None:
        with self._execution_lock:
            self.executing_robots.difference_update(str(item) for item in robot_ids)

    def executing_robot_snapshot(self) -> list[str]:
        with self._execution_lock:
            return sorted(self.executing_robots)

    def is_robot_executing(self, robot_id: str) -> bool:
        with self._execution_lock:
            return robot_id in self.executing_robots

    def push_log(self, level: str, msg: str, extra: dict = None):
        """追加日志并通过 WebSocket 广播。"""
        entry = {
            "ts": round(time.time() * 1000),
            "level": level,
            "msg": msg,
            **(extra or {}),
        }
        with self._log_lock:
            self.log_buffer.append(entry)
            if len(self.log_buffer) > 200:
                self.log_buffer.pop(0)
        socketio.emit("log", entry)

    def get_world_snapshot(self) -> dict:
        """返回当前世界状态（轻量版，供前端轮询/推送）。"""
        if not self.world_model:
            return {"robots": {}, "targets": []}
        state = self.world_model.get_world_state()
        return {
            "robots": state.get("robots", {}),
            "targets": state.get("targets", []),
            "timestamp": state.get("timestamp", 0),
        }


state = AppState()
_fleet_sync_lock = threading.Lock()
_adapter_reconnect_lock = threading.Lock()


def _adapter_connected(adapter) -> bool:
    """Return adapter connection state for method/property style adapters."""
    if adapter is None:
        return False
    connected = getattr(adapter, "is_connected", False)
    try:
        return bool(connected() if callable(connected) else connected)
    except Exception:
        return False


AIRSIM_CAMERA_CANDIDATES = {
    "front": [os.getenv("AIRSIM_CAMERA_FRONT"), "0", "front_center", "front", "cam_front", "CameraFront", "FPV", "fpv"],
    "right": [os.getenv("AIRSIM_CAMERA_RIGHT"), "1", "front_right", "right", "cam_right", "CameraRight"],
    "left": [os.getenv("AIRSIM_CAMERA_LEFT"), "2", "front_left", "left", "cam_left", "CameraLeft"],
    "down": [os.getenv("AIRSIM_CAMERA_DOWN"), "3", "bottom_center", "bottom", "down", "cam_down", "CameraDown"],
    "rear": [os.getenv("AIRSIM_CAMERA_REAR"), "4", "back_center", "rear", "back", "cam_rear", "cam_back", "CameraRear", "CameraBack"],
}
AIRSIM_SCENE_CAMERA_CANDIDATES = [
    os.getenv("AIRSIM_SCENE_CAMERA"),
    os.getenv("AIRSIM_EXTERNAL_CAMERA"),
    "overview",
    "global",
    "scene",
    "Scene",
    "external",
    "0",
]
_RESOLVED_AIRSIM_CAMERAS = {}
_RESOLVED_AIRSIM_SCENE_CAMERA = None
_LATEST_AIRSIM_FRAMES = {}
_LATEST_AIRSIM_SCENE = None
_LATEST_AIRSIM_FRAME_LOCK = threading.RLock()


def _camera_candidates(direction: str) -> list[str]:
    key = str(direction or "front").lower()
    raw_candidates = AIRSIM_CAMERA_CANDIDATES.get(key, AIRSIM_CAMERA_CANDIDATES["front"])
    seen = set()
    out = []
    resolved = _RESOLVED_AIRSIM_CAMERAS.get(key)
    for name in [resolved, *raw_candidates]:
        if name is None:
            continue
        name = str(name).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out or ["0"]


def _camera_name_for(direction: str) -> str:
    return _camera_candidates(direction)[0]


def _scene_camera_candidates() -> list[str]:
    seen = set()
    out = []
    for name in [_RESOLVED_AIRSIM_SCENE_CAMERA, *AIRSIM_SCENE_CAMERA_CANDIDATES]:
        if name is None:
            continue
        name = str(name).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out or ["0"]


def _airsim_image_to_jpeg_payload(
    response,
    camera_id: str,
    jpeg_quality: int = 92,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
):
    import cv2
    import numpy as np

    h = int(response.get("height", 0) or 0)
    w = int(response.get("width", 0) or 0)
    data = response.get("image_data_uint8") or response.get("image_data", b"")
    if isinstance(data, str):
        data = base64.b64decode(data)
    elif isinstance(data, list):
        data = bytes(data)
    if h <= 0 or w <= 0 or not data or len(data) < h * w * 3:
        return None
    img = np.frombuffer(data, dtype=np.uint8)[:h * w * 3].reshape(h, w, 3)
    if max_width or max_height:
        scale = 1.0
        if max_width:
            scale = min(scale, float(max_width) / float(w))
        if max_height:
            scale = min(scale, float(max_height) / float(h))
        if 0 < scale < 1.0:
            next_w = max(1, int(w * scale))
            next_h = max(1, int(h * scale))
            img = cv2.resize(img, (next_w, next_h), interpolation=cv2.INTER_AREA)
            h, w = next_h, next_w
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)])
    if not ok:
        return None
    return {
        "image": base64.b64encode(buf.tobytes()).decode("ascii"),
        "width": w,
        "height": h,
        "fps": 10.0,
        "camera_id": str(camera_id),
    }


def _airsim_fetch_image(
    rpc_client,
    camera_id: str,
    vehicle_name: str = "",
    external: bool = False,
    jpeg_quality: int = 92,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
):
    responses = rpc_client.sim_get_images([{
        "camera_name": camera_id,
        "image_type": 0,
        "pixels_as_float": False,
        "compress": False,
    }], vehicle_name=vehicle_name, external=external)
    if not responses:
        return None
    return _airsim_image_to_jpeg_payload(
        responses[0],
        camera_id,
        jpeg_quality=jpeg_quality,
        max_width=max_width,
        max_height=max_height,
    )


def _fetch_airsim_scene_camera(
    rpc_client,
    jpeg_quality: int = 85,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
):
    global _RESOLVED_AIRSIM_SCENE_CAMERA

    for camera_id in _scene_camera_candidates():
        try:
            result = _airsim_fetch_image(
                rpc_client,
                camera_id,
                vehicle_name="",
                external=True,
                jpeg_quality=jpeg_quality,
                max_width=max_width,
                max_height=max_height,
            )
        except Exception:
            continue
        if result:
            if _RESOLVED_AIRSIM_SCENE_CAMERA != camera_id:
                _RESOLVED_AIRSIM_SCENE_CAMERA = camera_id
                logger.info(f"AirSim scene camera resolved: {camera_id}")
            result.update({
                "view": "scene",
                "source": "airsim_external",
                "external": True,
            })
            return result
    return None


def _clamped_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _jpeg_bytes_from_payload(payload: Optional[dict]) -> Optional[bytes]:
    if not payload or not payload.get("image"):
        return None
    try:
        return base64.b64decode(payload["image"])
    except Exception:
        return None


def _cache_airsim_camera(direction: str, payload: dict) -> None:
    if not direction or not payload:
        return
    with _LATEST_AIRSIM_FRAME_LOCK:
        cached = dict(payload)
        cached["cached_at"] = time.time()
        _LATEST_AIRSIM_FRAMES[str(direction).lower()] = cached


def _get_cached_airsim_camera(direction: str, max_age: float = 3.0) -> Optional[dict]:
    with _LATEST_AIRSIM_FRAME_LOCK:
        payload = _LATEST_AIRSIM_FRAMES.get(str(direction or "front").lower())
        if not payload:
            return None
        if time.time() - float(payload.get("cached_at", 0.0)) > max_age:
            return None
        return dict(payload)


def _cache_airsim_scene(payload: dict) -> None:
    global _LATEST_AIRSIM_SCENE
    if not payload:
        return
    with _LATEST_AIRSIM_FRAME_LOCK:
        cached = dict(payload)
        cached["cached_at"] = time.time()
        _LATEST_AIRSIM_SCENE = cached


def _get_cached_airsim_scene(max_age: float = 8.0) -> Optional[dict]:
    with _LATEST_AIRSIM_FRAME_LOCK:
        if not _LATEST_AIRSIM_SCENE:
            return None
        if time.time() - float(_LATEST_AIRSIM_SCENE.get("cached_at", 0.0)) > max_age:
            return None
        return dict(_LATEST_AIRSIM_SCENE)


def _mjpeg_response(frame_getter, fps: int, name: str):
    delay = 1.0 / max(1, fps)

    @stream_with_context
    def generate():
        while True:
            started = time.time()
            frame = None
            try:
                frame = frame_getter()
            except GeneratorExit:
                return
            except Exception as exc:
                logger.debug("%s MJPEG frame failed: %s", name, exc)

            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-cache\r\n"
                    b"Content-Length: " + str(len(frame)).encode("ascii") + b"\r\n\r\n" +
                    frame + b"\r\n"
                )

            elapsed = time.time() - started
            if elapsed < delay:
                time.sleep(delay - elapsed)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )



# ══════════════════════════════════════════════════════════════════════════════
#  系统初始化
# ══════════════════════════════════════════════════════════════════════════════

def _build_robot_registry(robot_id: str, robot_type: str):
    """
    为单台机器人构建独立的 SkillRegistry，只注册该机器人类型支持的技能。
    每次调用都返回全新的实例（含独立的 Skill 对象），执行历史互不干扰。

    robot_type 匹配规则：
        skill.robot_type 为空列表 → 对所有类型可见（感知技能）
        否则 → robot_type 必须在列表中
    """
    from skills.registry import SkillRegistry
    from skills.motor_skills import (
        Takeoff, Land, FlyTo, Hover, GetPosition, GetBattery, ReturnToLaunch, ChangeAltitude,
        FlyRelative, LookAround, MarkLocation, GetMarks, OrbitInspect,
    )
    from skills.swarm_skills import (
        SwarmAreaSearch, SwarmRendezvous, SwarmFormationHold, SwarmOrbitHold,
    )
    from skills.mission_skills import (
        SwarmPerimeterPatrol, SwarmWaypointInspection,
        SwarmRelayDeploy, SwarmEscortRoute,
    )
    from skills.perception_skills import (
        DetectObject, RecognizeSpeech, FusePerception, ScanArea, GetSensorData, Observe, Perceive,
    )
    from skills.cognitive_skills import (
        RunPython, HttpRequest, ReadFile, WriteFile,
        Report, Alert, AskUser, UpdateMap,
    )

    # 全量技能工厂（每次都 new 出新实例，避免共享状态）
    ALL_SKILL_FACTORIES = [
        Takeoff, Land, FlyTo, FlyRelative, Hover, ChangeAltitude,
        GetPosition, GetBattery, ReturnToLaunch,
        LookAround, MarkLocation, GetMarks, OrbitInspect,
        SwarmAreaSearch, SwarmRendezvous, SwarmFormationHold, SwarmOrbitHold,
        SwarmPerimeterPatrol, SwarmWaypointInspection,
        SwarmRelayDeploy, SwarmEscortRoute,
        # 软技能不再注册 Python 类，改为文档驱动 (skills/soft_docs/*.md)
        DetectObject, RecognizeSpeech, FusePerception, ScanArea, GetSensorData, Observe, Perceive,
        # 认知技能（信息层）
        RunPython, HttpRequest, ReadFile, WriteFile,
        # 通信技能（主动交互）
        Report, Alert, AskUser, UpdateMap,
    ]

    reg = SkillRegistry(auto_generate_doc=False)
    count = 0
    for SkillClass in ALL_SKILL_FACTORIES:
        instance = SkillClass()
        rt = instance.robot_type  # list, e.g. ["UAV"] or ["UAV","UGV"] or []
        if not rt or robot_type in rt:
            reg.register_skill(instance)
            count += 1

    return reg, count


def _configured_uav_count(default: int) -> int:
    """Read the requested UAV count with a small, UI-friendly safety bound."""
    raw = os.getenv("AEROWEAVER_UAV_COUNT") or os.getenv("MOCK_UAV_COUNT") or str(default)
    try:
        count = int(raw)
    except (TypeError, ValueError):
        count = default
    return max(1, min(count, 12))


def _initial_robot_specs() -> list[tuple[str, str, list, float]]:
    """Build the initial robot roster.

    Mock mode is used by the demo UI, so it starts with a visible multi-UAV
    formation. Real adapters stay single-UAV unless explicitly configured.
    """
    sim_adapter = os.getenv("SIM_ADAPTER", "px4").lower()
    if sim_adapter in ("airsim", "airsim_physics"):
        count = state._desired_airsim_fleet_count
    else:
        default_count = 4 if sim_adapter == "mock" else 1
        count = _configured_uav_count(default_count)
    formation = [
        [0, 0, 0],
        [18, 0, 0],
        [18, 18, 0],
        [0, 18, 0],
        [36, 0, 0],
        [36, 18, 0],
        [0, 36, 0],
        [18, 36, 0],
        [36, 36, 0],
        [54, 0, 0],
        [54, 18, 0],
        [54, 36, 0],
    ]
    return [
        (f"UAV_{i + 1}", "UAV", formation[i % len(formation)], max(55.0, 92.0 - i * 3.0))
        for i in range(count)
    ]


def _do_init():
    """在后台线程中初始化所有模块，避免阻塞 HTTP 响应。"""
    global state
    try:
        state.push_log("info", "系统初始化中...")

        from memory.world_model import WorldModel
        from memory.episodic_memory import EpisodicMemory
        from memory.skill_memory import SkillMemory
        from runtime.agent_runtime import AgentRuntime
        from memory.reflection_engine import ReflectionEngine
        from memory.skill_evolution import SkillEvolution

        # ── 世界模型 ─────────────────────────────────────────────────────────
        state.world_model = WorldModel()
        robots_init = _initial_robot_specs()
        for rid, rtype, pos, bat in robots_init:
            state.world_model.register_robot(rid, rtype, initial_position=pos, battery=bat)
        state.push_log("success", f"世界模型初始化 ({', '.join(r[0] for r in robots_init)})")

        # ── 每台机器人独立注册表 ─────────────────────────────────────────────
        # 同类型的两台 UAV 各自拥有独立的 Skill 实例，执行历史互不干扰
        state.robot_registries = {}
        for rid, rtype, _, _ in robots_init:
            reg, count = _build_robot_registry(rid, rtype)
            state.robot_registries[rid] = reg
            state.push_log("info", f"  {rid} ({rtype}): 注册 {count} 个技能")

        total = sum(len(r) for r in state.robot_registries.values())
        state.push_log("success", f"技能注册完成：{len(state.robot_registries)} 台机器人，共 {total} 个技能实例")

        # ── 记忆模块 ─────────────────────────────────────────────────────────
        state.episodic_memory = EpisodicMemory()
        state.skill_memory = SkillMemory()

        # ── 反思引擎 + 技能进化 ──────────────────────────────────────────────
        try:
            from llm_client import get_client
            reflection_client = get_client(module="planner")
            reflection_engine = ReflectionEngine(
                llm_client=reflection_client,
                skill_memory=state.skill_memory,
            )
            skill_evolution = SkillEvolution(persist=True)
            state.push_log("success", "反思引擎 + 技能进化模块已加载")
        except Exception as e:
            reflection_engine = None
            skill_evolution = None
            state.push_log("warning", f"反思引擎加载失败(非致命): {e}")

        # ── 运行时（传入 per-robot 注册表字典）───────────────────────────────
        state.runtime = AgentRuntime(
            state.robot_registries,
            state.world_model,
            state.episodic_memory,
            state.skill_memory,
            reflection_engine=reflection_engine,
            skill_evolution=skill_evolution,
        )

        # ── 经验向量存储 ──────────────────────────────────────────────────────
        try:
            from memory.vector_store import VectorStore
            state.experience_store = VectorStore(collection="experiences")
            state.push_log("success", "经验向量存储已初始化")
        except Exception as e:
            state.experience_store = None
            state.push_log("warning", f"经验向量存储加载失败(非致命): {e}")

        state.initialized = True

        state.push_log("success", "✅ 系统初始化完成，等待设备接入")
        state.push_log("info", "💡 仿真设备: cd simulator && python sim_client.py")

        # 推送初始世界状态
        socketio.emit("world_state", state.get_world_snapshot())
        socketio.emit("skill_catalog", _get_skill_catalog())
        socketio.emit("system_status", _get_system_status())

        # 连接仿真适配器
        _try_connect_adapter()

    except Exception as e:
        logger.exception("初始化失败")
        state.push_log("error", f"初始化失败: {e}")


def _try_connect_adapter():
    """通过 adapter_manager 连接仿真环境，连上后启动遥测同步线程。"""
    def _connect():
        try:
            from adapters.adapter_manager import init_adapter, get_adapter
            import os
            sim_adapter = os.getenv("SIM_ADAPTER", "px4").lower()

            if sim_adapter == "airsim":
                host = os.getenv("AIRSIM_HOST", "127.0.0.1")
                port = os.getenv("AIRSIM_PORT", "41451")
                conn_str = f"{host}:{port}"
                state.push_log("info", f"Connecting to AirSim adapter ({conn_str})...")
                ok = init_adapter("airsim", connection_str=conn_str, timeout=15)
            elif sim_adapter == "airsim_physics":
                host = os.getenv("AIRSIM_HOST", "127.0.0.1")
                port = os.getenv("AIRSIM_PORT", "41451")
                conn_str = f"{host}:{port}"
                state.push_log("info", f"Connecting to AirSim Physics adapter ({conn_str})...")
                ok = init_adapter("airsim_physics", connection_str=conn_str, timeout=15)
            elif sim_adapter == "mavsdk":
                host = os.getenv("AIRSIM_HOST", "127.0.0.1")
                port = os.getenv("AIRSIM_PORT", "41451")
                conn_str = f"{host}:{port}"
                state.push_log("info", f"Connecting to MAVSDK+AirSim hybrid adapter (AirSim={conn_str})...")
                ok = init_adapter("mavsdk", connection_str=conn_str, timeout=20)
            elif sim_adapter == "mock":
                state.push_log("info", "Using mock adapter (no hardware)...")
                ok = init_adapter("mock", timeout=5)
            elif sim_adapter == "gazebo_direct":
                state.push_log("info", "Using Gazebo direct adapter (set_pose demo control)...")
                conn_str = os.getenv("GAZEBO_DIRECT_CONNECTION", "")
                ok = init_adapter("gazebo_direct", connection_str=conn_str, timeout=10)
            else:
                state.push_log("info", "Connecting to PX4 adapter (MAVSDK)...")
                ok = init_adapter("px4", connection_str=os.getenv("PX4_MAVSDK_URL", "udp://:14540"), timeout=int(os.getenv("PX4_CONNECT_TIMEOUT", "60")))

            adapter = get_adapter()
            if ok:
                if sim_adapter == "mock":
                    seed_fleet = getattr(adapter, "seed_fleet", None)
                    if callable(seed_fleet) and state.world_model:
                        seed_fleet(state.world_model.get_world_state().get("robots", {}))
                state.push_log("success", f"✅ Adapter connected: {adapter.name}")
                if sim_adapter in ("airsim", "airsim_physics"):
                    settle_fleet = getattr(adapter, "settle_active_fleet", None)
                    if callable(settle_fleet):
                        settle_result = settle_fleet(state._desired_airsim_fleet_count)
                        if settle_result.success:
                            state.push_log(
                                "success",
                                f"Active UAVs hovering at {settle_result.data.get('ground_clearance', {})}",
                            )
                        else:
                            state.push_log(
                                "warning",
                                f"Initial UAV descent unavailable: {settle_result.message}",
                            )
            else:
                state.push_log("warn", f"Adapter degraded to: {adapter.name}")
            _start_telemetry_sync()

            # Start simulator-specific sensor streaming.
            # AirSim frames are read via RPC; PX4+Gazebo frames/LiDAR are read
            # from Gazebo Transport topics by sim.gz_sensor_bridge.
            if sim_adapter in ("airsim", "airsim_physics") and ok:
                _start_airsim_camera_stream()
                # 启动被动感知引擎
                _start_passive_perception()
            elif sim_adapter in ("px4", "gazebo", "gz", "gazebo_direct") or os.getenv("AEROWEAVER_FORCE_GZ_SENSOR_BRIDGE") == "1":
                # Gazebo camera/LiDAR topics are independent from MAVSDK control connectivity.
                # Start the sensor bridge even if the PX4 adapter is still connecting or degraded,
                # so the Web UI can show the AeroWeaver modified UAV sensors as soon as Gazebo is up.
                _start_sensor_bridge()

        except Exception as e:
            state.push_log("warn", f"Adapter unavailable: {e}, running in mock mode")

    t = threading.Thread(target=_connect, daemon=True)
    t.start()


def _vehicle_sort_key(name: str):
    import re
    text = str(name or "")
    match = re.search(r"(\d+)$", text)
    return (int(match.group(1)) if match else 10**9, text)


def _state_from_airsim_raw(
    adapter,
    vehicle_name: str,
    fallback_state=None,
    refresh_clearance: bool = True,
):
    """Read one AirSim vehicle and normalize it into WorldModel robot fields."""
    read_clearance = getattr(adapter, "get_ground_clearance", None)
    ground_clearance = (
        read_clearance(vehicle_name)
        if refresh_clearance and callable(read_clearance)
        else None
    )
    if ground_clearance is None:
        ground_clearance = getattr(adapter, "_ground_clearance", {}).get(vehicle_name)
    primary = getattr(adapter, "_vehicle_name", "")
    if vehicle_name == primary and fallback_state is not None and getattr(fallback_state, "position_ned", None):
        pos = fallback_state.position_ned
        in_air = bool(getattr(fallback_state, "in_air", False))
        battery = float(getattr(fallback_state, "battery_percent", 100.0) or 100.0)
        return (
            [round(pos.north, 2), round(pos.east, 2), round(pos.down, 2)],
            in_air,
            battery,
            ground_clearance,
        )

    client = getattr(adapter, "_client", None)
    if not client:
        return None
    if hasattr(client, "sim_get_object_pose"):
        pose = client.sim_get_object_pose(vehicle_name) or {}
        raw_pos = pose.get("position", {})
    else:
        raw = client.get_multirotor_state(vehicle_name) or {}
        raw_pos = raw.get("kinematics_estimated", {}).get("position", {})
    x = float(raw_pos.get("x_val", 0.0))
    y = float(raw_pos.get("y_val", 0.0))
    z = float(raw_pos.get("z_val", 0.0))
    return (
        [round(x, 2), round(y, 2), round(z, 2)],
        z < -1.0,
        100.0,
        ground_clearance,
    )


def _parallel_airsim_fleet_samples(
    adapter,
    vehicles,
    refresh_clearance: bool = True,
):
    """Read all active vehicle poses concurrently over independent RPC sockets."""
    from concurrent.futures import ThreadPoolExecutor
    from adapters.airsim_rpc import AirSimDirectClient

    if not vehicles:
        return {}

    if not hasattr(adapter, "_telemetry_client_lock"):
        adapter._telemetry_client_lock = threading.Lock()
    if not hasattr(adapter, "_telemetry_clients"):
        adapter._telemetry_clients = {}
    if not hasattr(adapter, "_telemetry_pool"):
        adapter._telemetry_pool = ThreadPoolExecutor(
            max_workers=12,
            thread_name_prefix="airsim-pose",
        )

    host = getattr(adapter, "_airsim_host", os.getenv("AIRSIM_HOST", "127.0.0.1"))
    port = int(getattr(adapter, "_airsim_port", os.getenv("AIRSIM_PORT", "41451")))
    endpoint = (host, port)
    distance_sensor = os.getenv("AIRSIM_BOTTOM_DISTANCE_SENSOR", "BottomDistance")

    def _client_for(vehicle_name):
        with adapter._telemetry_client_lock:
            entry = adapter._telemetry_clients.get(vehicle_name)
            if entry and entry[:2] == endpoint:
                return entry[2]
            if entry:
                try:
                    entry[2].close()
                except Exception:
                    pass
            client = AirSimDirectClient(host, port, timeout=2.0)
            if not client.connect():
                raise ConnectionError(
                    f"AirSim telemetry connection failed for {vehicle_name}"
                )
            adapter._telemetry_clients[vehicle_name] = (*endpoint, client)
            return client

    def _drop_client(vehicle_name, client):
        with adapter._telemetry_client_lock:
            entry = adapter._telemetry_clients.get(vehicle_name)
            if entry and entry[2] is client:
                adapter._telemetry_clients.pop(vehicle_name, None)
        try:
            client.close()
        except Exception:
            pass

    def _read_one(vehicle_name):
        client = _client_for(vehicle_name)
        try:
            pose = client.sim_get_object_pose(vehicle_name) or {}
            raw_pos = pose.get("position", {})
            x = float(raw_pos.get("x_val", 0.0))
            y = float(raw_pos.get("y_val", 0.0))
            z = float(raw_pos.get("z_val", 0.0))

            ground_clearance = getattr(
                adapter,
                "_ground_clearance",
                {},
            ).get(vehicle_name)
            if refresh_clearance:
                reading = client.get_distance_sensor_data(
                    distance_sensor,
                    vehicle_name,
                ) or {}
                try:
                    distance = float(reading.get("distance"))
                    maximum = float(
                        reading.get("max_distance", 100.0) or 100.0
                    )
                except (TypeError, ValueError):
                    distance = None
                    maximum = 100.0
                if distance is not None and 0.0 < distance < maximum * 0.95:
                    ground_clearance = round(distance, 3)
                    adapter._ground_clearance[vehicle_name] = ground_clearance

            return (
                [round(x, 2), round(y, 2), round(z, 2)],
                z < -1.0,
                100.0,
                ground_clearance,
            )
        except Exception:
            _drop_client(vehicle_name, client)
            raise

    futures = {
        vehicle: adapter._telemetry_pool.submit(_read_one, vehicle)
        for vehicle in vehicles
    }
    samples = {}
    for vehicle, future in futures.items():
        try:
            samples[vehicle] = future.result(timeout=2.5)
        except Exception as exc:
            logger.debug("Parallel AirSim telemetry failed for %s: %s", vehicle, exc)
    return samples


def _sync_airsim_fleet_to_world(
    adapter,
    fallback_state=None,
    refresh_clearance: bool = True,
) -> bool:
    """Replace UAV robots with the real AirSim vehicle inventory and positions."""
    if not state.world_model:
        return False
    client = getattr(adapter, "_client", None)
    if not client:
        return False

    vehicles = list(getattr(adapter, "_vehicle_names", []) or [])
    if not vehicles:
        try:
            vehicles = list(client.list_vehicles() or [])
        except Exception:
            vehicles = []
    primary = getattr(adapter, "_vehicle_name", "")
    if primary and primary not in vehicles:
        vehicles.insert(0, primary)
    vehicles = sorted([str(v) for v in vehicles if str(v).strip()], key=_vehicle_sort_key)
    if not vehicles:
        return False
    state._airsim_pool_vehicle_count = len(vehicles)
    active_count = max(
        1,
        min(int(getattr(state, "_desired_airsim_fleet_count", 1)), len(vehicles)),
    )
    vehicles = vehicles[:active_count]
    parallel_samples = _parallel_airsim_fleet_samples(
        adapter,
        vehicles,
        refresh_clearance=refresh_clearance,
    )

    existing = state.world_model.get_world_state().get("robots", {})
    robots = {}
    for index, vehicle in enumerate(vehicles):
        robot_id = f"UAV_{index + 1}"
        try:
            parsed = parallel_samples.get(vehicle)
            if parsed is None:
                parsed = _state_from_airsim_raw(
                    adapter,
                    vehicle,
                    fallback_state=fallback_state,
                    refresh_clearance=refresh_clearance,
                )
        except Exception as exc:
            logger.debug("AirSim fleet state read failed for %s: %s", vehicle, exc)
            parsed = None
        if not parsed:
            continue
        position, in_air, battery, ground_clearance = parsed
        current = existing.get(robot_id, {})
        status = (
            "executing"
            if state.is_robot_executing(robot_id)
            else ("airborne" if in_air else "idle")
        )
        robots[robot_id] = {
            "robot_type": "UAV",
            "position": position,
            "battery": battery,
            "status": status,
            "in_air": in_air,
            "armed": True,
            "source": "airsim",
            "airsim_vehicle": vehicle,
            "ground_clearance": ground_clearance,
            "sensor_status": {
                "camera": True,
                "lidar": True,
                "distance": ground_clearance is not None,
                "imu": True,
                "gps": True,
            },
        }

    if not robots:
        return False

    non_uav = {
        rid: data
        for rid, data in existing.items()
        if not str(rid).upper().startswith("UAV_")
    }
    state.world_model._state["robots"] = {**non_uav, **robots}
    state.world_model._state["timestamp"] = time.time()

    live_ids = set(robots.keys())
    for robot_id in live_ids:
        if robot_id not in state.robot_registries:
            reg, _ = _build_robot_registry(robot_id, "UAV")
            state.robot_registries[robot_id] = reg
    for robot_id in list(state.robot_registries.keys()):
        if str(robot_id).upper().startswith("UAV_") and robot_id not in live_ids:
            state.robot_registries.pop(robot_id, None)
    if state.runtime:
        state.runtime._robot_registries = state.robot_registries
        state.runtime._executor._robot_registries = state.robot_registries

    if state.current_robot not in live_ids:
        state.current_robot = sorted(live_ids, key=_vehicle_sort_key)[0]

    signature = tuple((rid, data["airsim_vehicle"]) for rid, data in sorted(robots.items()))
    if getattr(state, "_airsim_fleet_signature", None) != signature:
        state._airsim_fleet_signature = signature
        logger.info("AirSim fleet synced: %s", ", ".join(f"{rid}={data['airsim_vehicle']}" for rid, data in sorted(robots.items())))
        socketio.emit("skill_catalog", _get_skill_catalog())
        socketio.emit("system_status", _get_system_status())
    return True


def _start_telemetry_sync():
    """后台持续读取仿真遥测数据，同步到 WorldModel 并推送前端。"""
    sim_adapter = os.getenv("SIM_ADAPTER", "px4").lower()
    default_position_hz = 10.0 if sim_adapter in ("airsim", "airsim_physics", "mock") else 2.0
    try:
        position_hz = float(
            os.getenv("AIRSIM_TELEMETRY_HZ", str(default_position_hz))
        )
    except (TypeError, ValueError):
        position_hz = default_position_hz
    try:
        clearance_hz = float(os.getenv("AIRSIM_CLEARANCE_HZ", "2"))
    except (TypeError, ValueError):
        clearance_hz = 2.0
    position_hz = max(1.0, min(position_hz, 30.0))
    clearance_hz = max(0.5, min(clearance_hz, position_hz))
    position_interval = 1.0 / position_hz
    clearance_interval = 1.0 / clearance_hz

    def _sync_loop():
        from adapters.adapter_manager import get_adapter
        _reconnect_attempts = 0
        _MAX_RECONNECT_INTERVAL = 30  # 最大重连间隔(秒)
        last_clearance_refresh = 0.0

        while state.initialized:
            loop_started = time.monotonic()
            try:
                adapter = get_adapter()
                if adapter and not _adapter_connected(adapter):
                    # mavsdk_server 可能崩了，尝试自动重连
                    _reconnect_attempts += 1
                    wait = min(5 * _reconnect_attempts, _MAX_RECONNECT_INTERVAL)
                    if _reconnect_attempts <= 3 or _reconnect_attempts % 10 == 0:
                        logger.warning(f"Adapter ?????{wait}?????{_reconnect_attempts}???...")
                        state.push_log("warning", f"?? Adapter ????????? (?{_reconnect_attempts}?)...")
                    time.sleep(wait)
                    conn_str = ""
                    timeout = 15
                    if sim_adapter in ("airsim", "airsim_physics", "mavsdk"):
                        conn_str = f"{os.getenv('AIRSIM_HOST', '127.0.0.1')}:{os.getenv('AIRSIM_PORT', '41451')}"
                    elif sim_adapter == "px4":
                        conn_str = os.getenv("PX4_MAVSDK_URL", "udp://:14540")
                        timeout = int(os.getenv("PX4_CONNECT_TIMEOUT", "60"))
                    elif sim_adapter == "gazebo_direct":
                        conn_str = os.getenv("GAZEBO_DIRECT_CONNECTION", "")
                    with _adapter_reconnect_lock:
                        if _adapter_connected(adapter):
                            ok = True
                        else:
                            ok = adapter.connect(connection_str=conn_str, timeout=timeout) if conn_str else adapter.connect(timeout=timeout)
                    if ok:
                        logger.info("Adapter ??????")
                        state.push_log("success", "? Adapter ??????")
                        _reconnect_attempts = 0
                    continue
                if adapter and _adapter_connected(adapter):
                    _reconnect_attempts = 0
                    if sim_adapter in ("airsim", "airsim_physics"):
                        now = time.monotonic()
                        refresh_clearance = (
                            now - last_clearance_refresh >= clearance_interval
                        )
                        if refresh_clearance:
                            last_clearance_refresh = now
                        if _sync_airsim_fleet_to_world(
                            adapter,
                            refresh_clearance=refresh_clearance,
                        ):
                            socketio.emit("world_state", state.get_world_snapshot())
                            elapsed = time.monotonic() - loop_started
                            time.sleep(max(0.0, position_interval - elapsed))
                            continue

                    if sim_adapter == "mock":
                        get_snapshot = getattr(adapter, "get_robot_snapshot", None)
                        fleet = get_snapshot() if callable(get_snapshot) else {}
                        available = state.world_model.get_world_state().get("robots", {})
                        update = {"robots": {}}
                        for robot_id, telemetry in fleet.items():
                            if robot_id not in available:
                                continue
                            raw_battery = float(telemetry.get("battery", 1.0))
                            battery = raw_battery * 100.0 if raw_battery <= 1.0 else raw_battery
                            moving = bool(telemetry.get("moving", False))
                            in_air = bool(telemetry.get("in_air", False))
                            update["robots"][robot_id] = {
                                "battery": round(max(0.0, min(100.0, battery)), 1),
                                "position": [round(float(value), 2) for value in telemetry.get("position", [0, 0, 0])[:3]],
                                "in_air": in_air,
                                "status": "executing" if moving or state.is_robot_executing(robot_id) else ("airborne" if in_air else "idle"),
                            }
                        if update["robots"]:
                            state.world_model.update_world_state(update)
                            socketio.emit("world_state", state.get_world_snapshot())
                        elapsed = time.monotonic() - loop_started
                        time.sleep(max(0.0, position_interval - elapsed))
                        continue

                    st = adapter.get_state()
                    robot_id = "UAV_1"
                    update = {"robots": {robot_id: {}}}

                    if st.battery_percent > 0:
                        raw = st.battery_percent
                        # MAVSDK remaining_percent: 通常 0-100
                        # 但 PX4 SITL 有时返回异常值, 做多层兜底
                        if raw > 100:
                            pct = raw / 100.0   # 可能是 0-10000 的万分比
                            if pct > 100:
                                pct = 100.0     # 仍然超 100, 封顶
                        elif raw <= 1.0:
                            pct = raw * 100.0   # 0-1 范围, 转百分比
                        else:
                            pct = raw           # 正常 0-100
                        pct = max(0.0, min(100.0, round(pct, 1)))
                        update["robots"][robot_id]["battery"] = pct
                    if st.position_ned:
                        p = st.position_ned
                        update["robots"][robot_id]["position"] = [round(p.north, 2), round(p.east, 2), round(p.down, 2)]
                    # in_air 始终更新（执行期间 LLM 也需要知道飞行状态）
                    update["robots"][robot_id]["in_air"] = st.in_air
                    if not state.is_robot_executing(robot_id):
                        update["robots"][robot_id]["status"] = "airborne" if st.in_air else "idle"

                    state.world_model.update_world_state(update)
                    socketio.emit("world_state", state.get_world_snapshot())
            except Exception:
                pass
            elapsed = time.monotonic() - loop_started
            time.sleep(max(0.0, position_interval - elapsed))

    t = threading.Thread(target=_sync_loop, daemon=True, name="telemetry-sync")
    t.start()
    state.push_log(
        "info",
        (
            f"Telemetry synchronization started "
            f"({position_hz:g}Hz position, {clearance_hz:g}Hz distance/status)"
        ),
    )


def _start_passive_perception():
    """启动被动感知引擎：定期 VLM 分析摄像头画面，更新 WorldModel。"""
    try:
        from perception.passive_perception import PassivePerception
        from perception.vlm_analyzer import get_analyzer, init_analyzer
        from skills.perception_skills import set_passive_perception
        from adapters.adapter_manager import get_adapter as _get_adapter_fn

        analyzer = get_analyzer()
        if analyzer is None:
            analyzer = init_analyzer()

        engine = PassivePerception(
            adapter_getter=_get_adapter_fn,
            world_model=state.world_model,
            vlm_analyzer=analyzer,
            socketio=socketio,
            interval_seconds=8.0,  # 每 8 秒分析一次（避免 VLM API 过载）
        )
        engine.start()
        set_passive_perception(engine)
        state.push_log("info", "👁️ 被动感知引擎已启动 (8s/次)")
        logger.info("被动感知引擎已启动")
    except Exception as e:
        logger.warning(f"被动感知引擎启动失败: {e}")
        state.push_log("warn", f"⚠️ 被动感知引擎未启动: {e}")


def _start_airsim_camera_stream():
    """Push AirSim camera frames using the same WebSocket payload as Gazebo."""
    if AIRSIM_CAMERA_RELAY_ENABLED:
        logger.info(
            "AirSim browser camera feed uses local relay at %s; "
            "legacy cross-host raw-frame polling is disabled",
            AIRSIM_CAMERA_RELAY_URL,
        )
        return

    stream_interval = max(0.05, float(os.getenv("AIRSIM_CAMERA_STREAM_INTERVAL", "0.25")))
    scene_interval = max(0.5, float(os.getenv("AIRSIM_SCENE_STREAM_INTERVAL", "3.0")))
    camera_quality = _clamped_int(os.getenv("AIRSIM_SOCKET_JPEG_QUALITY"), 75, 35, 95)
    camera_max_width = _clamped_int(os.getenv("AIRSIM_SOCKET_MAX_WIDTH"), 960, 160, 2560)
    camera_max_height = _clamped_int(os.getenv("AIRSIM_SOCKET_MAX_HEIGHT"), 540, 120, 1440)
    scene_quality = _clamped_int(os.getenv("AIRSIM_SOCKET_SCENE_JPEG_QUALITY"), 75, 35, 95)
    scene_max_width = _clamped_int(os.getenv("AIRSIM_SOCKET_SCENE_MAX_WIDTH"), 1280, 160, 3840)
    scene_max_height = _clamped_int(os.getenv("AIRSIM_SOCKET_SCENE_MAX_HEIGHT"), 720, 120, 2160)

    def _airsim_stream_loop():
        from adapters.adapter_manager import get_adapter

        logger.info("AirSim camera stream thread started with camera-id probing")
        fail_count = 0
        frame_counter = 0
        last_scene_emit = 0.0
        _cached_frames = {}
        _cam_rpc = None

        def _fetch_one_camera(rpc_client, cam_id, vehicle):
            try:
                return _airsim_fetch_image(
                    rpc_client,
                    cam_id,
                    vehicle_name=vehicle,
                    jpeg_quality=camera_quality,
                    max_width=camera_max_width,
                    max_height=camera_max_height,
                )
            except Exception:
                pass
            return None

        def _fetch_direction_camera(rpc_client, direction, vehicle):
            for cam_id in _camera_candidates(direction):
                result = _fetch_one_camera(rpc_client, cam_id, vehicle)
                if result:
                    if _RESOLVED_AIRSIM_CAMERAS.get(direction) != cam_id:
                        _RESOLVED_AIRSIM_CAMERAS[direction] = cam_id
                        logger.info(f"AirSim camera resolved: {direction} -> {cam_id}")
                    return result
            return None

        while state.initialized:
            try:
                adapter = get_adapter()
                if not adapter or not getattr(adapter, '_connected', False):
                    time.sleep(1)
                    continue

                if _cam_rpc is None:
                    try:
                        from adapters.airsim_rpc import AirSimDirectClient
                        ip = getattr(adapter, '_airsim_host', os.getenv('AIRSIM_HOST', '127.0.0.1'))
                        port = int(getattr(adapter, '_airsim_port', os.getenv('AIRSIM_PORT', '41451')))
                        _cam_rpc = AirSimDirectClient(ip, port, timeout=5)
                        if not _cam_rpc.connect():
                            raise ConnectionError(f"Cannot connect to AirSim camera RPC at {ip}:{port}")
                        logger.info(f"AirSim camera RPC connected: {ip}:{port}")
                    except Exception as ce:
                        logger.warning(f"AirSim camera RPC failed, using main client: {ce}")
                        _cam_rpc = getattr(adapter, '_client', None)

                rpc_client = _cam_rpc or getattr(adapter, '_client', None)
                if not rpc_client:
                    time.sleep(1)
                    continue

                frame_counter += 1
                cameras_payload = {}
                vehicle = getattr(adapter, '_vehicle_name', '')

                active_dirs = ["front"]
                other_dirs = [d for d in AIRSIM_CAMERA_CANDIDATES.keys() if d != "front"]
                if other_dirs and frame_counter % 5 == 0:
                    active_dirs.append(other_dirs[(frame_counter // 5) % len(other_dirs)])

                for direction in active_dirs:
                    result = _fetch_direction_camera(rpc_client, direction, vehicle)
                    if result:
                        cameras_payload[direction] = result
                        _cached_frames[direction] = result
                        _cache_airsim_camera(direction, result)

                for direction in AIRSIM_CAMERA_CANDIDATES.keys():
                    if direction not in cameras_payload and direction in _cached_frames:
                        cameras_payload[direction] = _cached_frames[direction]

                if cameras_payload:
                    socketio.emit("sensor_cameras", cameras_payload)
                    if "front" in cameras_payload:
                        socketio.emit("sensor_camera", cameras_payload["front"])
                    fail_count = 0
                else:
                    fail_count += 1
                    if fail_count == 1:
                        logger.warning("AirSim camera returned no data while probing camera ids")

                now_ts = time.time()
                if now_ts - last_scene_emit >= scene_interval:
                    scene_payload = _fetch_airsim_scene_camera(
                        rpc_client,
                        jpeg_quality=scene_quality,
                        max_width=scene_max_width,
                        max_height=scene_max_height,
                    )
                    if scene_payload:
                        _cache_airsim_scene(scene_payload)
                        socketio.emit("sensor_scene", scene_payload)
                        last_scene_emit = now_ts

            except Exception as e:
                logger.debug(f"AirSim camera stream error: {e}")

            _lidar_counter = getattr(_airsim_stream_loop, "_lc", 0) + 1
            _airsim_stream_loop._lc = _lidar_counter
            if _lidar_counter % 15 != 0:
                time.sleep(stream_interval)
                continue
            try:
                import math
                depth_cameras = {
                    _camera_name_for("front"): 0,
                    _camera_name_for("right"): 90,
                    _camera_name_for("left"): -90,
                    _camera_name_for("rear"): 180,
                }
                max_range = 100.0
                num_bins = 360
                ranges = [max_range] * num_bins

                for cam_id, yaw_deg in depth_cameras.items():
                    try:
                        depth_resp = rpc_client.sim_get_images([{
                            "camera_name": cam_id,
                            "image_type": 2,
                            "pixels_as_float": True,
                            "compress": False,
                        }], vehicle_name=vehicle)
                        if not depth_resp:
                            continue
                        dr = depth_resp[0]
                        dh = int(dr.get("height", 0) or 0)
                        dw = int(dr.get("width", 0) or 0)
                        depth_data = dr.get("image_data_float") or dr.get("image_data_uint8") or []
                        if not depth_data or dh == 0 or dw == 0:
                            continue
                        if isinstance(depth_data, list):
                            depth_arr = depth_data
                        else:
                            import struct
                            depth_arr = list(struct.unpack(f'{len(depth_data)//4}f', depth_data))
                        mid_row = dh // 2
                        row_start = mid_row * dw
                        row_end = row_start + dw
                        if row_end <= len(depth_arr):
                            row_depths = depth_arr[row_start:row_end]
                            fov = 90.0
                            for col_idx, depth in enumerate(row_depths):
                                if depth <= 0 or depth >= max_range:
                                    continue
                                col_frac = (col_idx - dw / 2) / (dw / 2)
                                angle_offset = col_frac * (fov / 2)
                                angle_deg = (yaw_deg + angle_offset) % 360
                                bin_idx = int(angle_deg) % num_bins
                                h_dist = depth * math.cos(math.radians(angle_offset))
                                if h_dist < ranges[bin_idx]:
                                    ranges[bin_idx] = round(h_dist, 2)
                    except Exception:
                        pass

                clean_ranges = [r if r < max_range else max_range for r in ranges]
                socketio.emit("sensor_lidar", {
                    "is_3d": False,
                    "ranges": clean_ranges,
                    "angle_min": -math.pi,
                    "angle_max": math.pi,
                    "angle_increment": 2 * math.pi / num_bins,
                    "range_min": 0.1,
                    "range_max": max_range,
                    "count": num_bins,
                    "fps": 5.0,
                })
            except Exception as lidar_e:
                if fail_count < 2:
                    logger.warning(f"AirSim depth-image LiDAR failed: {lidar_e}")

            time.sleep(stream_interval)

    t = threading.Thread(target=_airsim_stream_loop, daemon=True, name="airsim-camera-stream")
    t.start()
    state.push_log("info", "AirSim camera stream started; use Sensor/visible-light to view frames")


def _start_sensor_bridge():
    """启动 Gazebo 传感器桥接，开始推送相机和雷达数据到前端。"""
    def _init_bridge():
        try:
            from sim.gz_sensor_bridge import GzSensorBridge
            from skills.perception_skills import set_sensor_bridge

            # 从 start.py 传入的环境变量读取 world 名
            world = os.environ.get("PX4_GZ_WORLD", "urban_rescue")
            model = os.environ.get("PX4_SIM_MODEL", "x500_lidar_2d_cam") + "_0"
            bridge = GzSensorBridge(model_name=model, world_name=world)

            if bridge.start():
                state.sensor_bridge = bridge
                set_sensor_bridge(bridge)
                state.push_log("success", f"📷 传感器桥接启动 (world={world}, model={model})")
                logger.info("开始启动传感器数据推送线程...")
                # 启动数据推送线程
                try:
                    _start_sensor_stream()
                    logger.info("传感器数据推送线程启动成功")
                except Exception as e:
                    logger.error(f"传感器数据推送线程启动失败: {e}", exc_info=True)

                # 生成 BODY.md (身体认知文档)
                _generate_body_md()

                # 启动感知守护线程
                _start_perception_daemon()
            else:
                state.push_log("warn", "传感器桥接启动失败（Gazebo 可能未运行）")

        except ImportError as e:
            state.push_log("warn", f"传感器桥接不可用: {e}")
            logger.error(f"传感器桥接导入失败: {e}", exc_info=True)
        except Exception as e:
            state.push_log("warn", f"传感器桥接异常: {e}")
            logger.error(f"传感器桥接异常: {e}", exc_info=True)

    def _generate_body_md():
        """生成 BODY.md 身体认知文档。"""
        try:
            from robot_profile.body_generator import generate_body_md
            from adapters.adapter_manager import get_adapter
            adapter = get_adapter()
            skill_reg = None
            for rid, reg in state.robot_registries.items():
                skill_reg = reg
                break
            generate_body_md(
                adapter=adapter,
                sensor_bridge=state.sensor_bridge,
                skill_registry=skill_reg,
            )
            state.push_log("info", "BODY.md 身体认知文档已生成")
        except Exception as e:
            logger.warning("BODY.md 生成失败: %s", e)

    def _start_perception_daemon():
        """启动感知守护线程。"""
        try:
            from perception.daemon import init_daemon
            from adapters.adapter_manager import get_adapter
            adapter = get_adapter()
            daemon = init_daemon(
                sensor_bridge=state.sensor_bridge,
                adapter=adapter,
                update_interval=3.0,
            )
            state.push_log("info", "感知守护线程已启动 (3s 间隔)")
        except Exception as e:
            logger.warning("感知守护线程启动失败: %s", e)
            state.push_log("warn", f"感知守护线程启动失败: {e}")

    def _spawn_camera_dynamic(world: str):
        """动态 spawn OakD-Lite 相机到 Gazebo 世界"""
        try:
            import subprocess

            logger.info(f"开始动态 spawn 相机到 world={world}")

            # 用 SDF 文件 spawn（避免引号转义问题）
            sdf_file = os.path.join(_BASE_DIR, "config", "camera_spawn.sdf")
            with open(sdf_file, "r") as f:
                sdf_content = f.read()

            cmd = [
                "gz", "service",
                "-s", f"/world/{world}/create",
                "--reqtype", "gz.msgs.EntityFactory",
                "--reptype", "gz.msgs.Boolean",
                "--timeout", "5000",
                "--req", f'sdf: "{sdf_content}"'
            ]
            logger.info(f"执行 gz service spawn 相机")
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            logger.info(f"spawn 结果: rc={result.returncode}, stdout={result.stdout.decode()}, stderr={result.stderr.decode()}")
            if result.returncode == 0 and b"true" in result.stdout:
                state.push_log("success", "📷 OakD-Lite 相机已动态 spawn 到世界")
            else:
                state.push_log("warn", f"相机 spawn 失败: rc={result.returncode} {result.stderr.decode()}")

        except Exception as e:
            logger.error(f"相机 spawn 异常: {e}", exc_info=True)
            state.push_log("warn", f"相机 spawn 异常: {e}")

    # 延迟 15 秒等 Gazebo + PX4 完全启动
    def _delayed_init():
        time.sleep(15)
        _init_bridge()

    t = threading.Thread(target=_delayed_init, daemon=True, name="sensor-bridge-init")
    t.start()


def _start_sensor_stream():
    """后台线程：周期性推送 4 相机 + 激光雷达数据到前端 WebSocket。"""
    import base64
    import cv2
    import math

    DIRECTIONS = ["front", "rear", "left", "right", "down"]

    def _stream_loop():
        while state.initialized and state.sensor_bridge and state.sensor_bridge.is_running:
            try:
                bridge = state.sensor_bridge

                # ── 4 相机帧 ──
                cameras_payload = {}
                for d in DIRECTIONS:
                    img = bridge.get_camera_image(d)
                    if img is not None:
                        _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
                        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
                        info = bridge.get_camera_info(d)
                        cameras_payload[d] = {
                            "image": b64,
                            "width": info["width"],
                            "height": info["height"],
                            "fps": round(info["fps"], 1),
                        }
                if cameras_payload:
                    socketio.emit("sensor_cameras", cameras_payload)

                # 兼容旧前端：也发 sensor_camera (front)
                if "front" in cameras_payload:
                    socketio.emit("sensor_camera", cameras_payload["front"])

                # ── 激光雷达 ──
                scan = bridge.get_lidar_scan()
                if scan is not None:
                    ranges = scan["ranges"]
                    rmax = scan["range_max"]
                    rmin = scan["range_min"]
                    v_count = scan.get("vertical_count", 1)
                    h_count = scan.get("count", len(ranges))
                    is_3d = scan.get("is_3d", False)

                    if is_3d and v_count > 1:
                        # 3D 点云: ranges 按 [h0v0, h0v1, ..., h0vN, h1v0, ...] 排列
                        # 每个水平角度有 v_count 个垂直采样
                        # 降采样水平方向到最多 180 线
                        h_step = max(1, h_count // 180)
                        v_angle_min = scan.get("vertical_angle_min", 0)
                        v_angle_max = scan.get("vertical_angle_max", 0)

                        # 提取每层的水平扫描
                        layers = []
                        for vi in range(v_count):
                            layer_ranges = []
                            for hi in range(0, h_count, h_step):
                                idx = hi * v_count + vi
                                if idx < len(ranges):
                                    r = ranges[idx]
                                    layer_ranges.append(
                                        round(r, 2) if (math.isfinite(r) and r >= rmin) else rmax
                                    )
                            layers.append(layer_ranges)

                        socketio.emit("sensor_lidar", {
                            "is_3d": True,
                            "layers": layers,
                            "h_count": len(layers[0]) if layers else 0,
                            "v_count": v_count,
                            "angle_min": scan["angle_min"],
                            "angle_max": scan["angle_max"],
                            "angle_increment": scan["angle_increment"] * h_step,
                            "v_angle_min": v_angle_min,
                            "v_angle_max": v_angle_max,
                            "range_min": rmin,
                            "range_max": rmax,
                            "count": len(layers[0]) if layers else 0,
                            "total_points": scan.get("total_points", len(ranges)),
                            "fps": round(bridge.get_lidar_info()["fps"], 1),
                        })
                    else:
                        # 2D 兼容模式
                        step = max(1, len(ranges) // 270)
                        actual_increment = scan["angle_increment"] * step
                        clean_ranges = [
                            round(r, 2) if (math.isfinite(r) and r >= rmin) else rmax
                            for r in ranges[::step]
                        ]
                        socketio.emit("sensor_lidar", {
                            "is_3d": False,
                            "ranges": clean_ranges,
                            "angle_min": scan["angle_min"],
                            "angle_max": scan["angle_max"],
                            "angle_increment": actual_increment,
                            "range_min": rmin,
                            "range_max": rmax,
                            "count": len(clean_ranges),
                            "fps": round(bridge.get_lidar_info()["fps"], 1),
                        })

            except Exception as e:
                logger.debug(f"传感器推送异常: {e}")

            time.sleep(0.1)  # 10 FPS

    t = threading.Thread(target=_stream_loop, daemon=True, name="sensor-stream")
    t.start()
    state.push_log("info", "传感器数据推送线程已启动（10Hz 4相机/雷达）")


def _get_skill_catalog(robot_id: str = None) -> dict:
    """
    返回技能表。
    - robot_id 指定时：返回该机器人的技能列表（list）
    - robot_id 为 None 时：返回所有机器人的技能表字典 {robot_id: [skills]}
    """
    if not state.robot_registries:
        return {} if robot_id is None else []
    if robot_id:
        reg = state.robot_registries.get(robot_id)
        return reg.get_skill_catalog() if reg else []
    return {
        rid: reg.get_skill_catalog()
        for rid, reg in state.robot_registries.items()
    }


def _get_system_status() -> dict:
    executing_robots = state.executing_robot_snapshot()
    return {
        "initialized": state.initialized,
        "mode": state.mode,
        "is_executing": state.is_executing or bool(executing_robots),
        "ai_executing": state.is_executing,
        "executing_robots": executing_robots,
        "current_robot": state.current_robot,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  REST API
# ══════════════════════════════════════════════════════════════════════════════

_REQUIRED_DEVICE_FIELDS = ("device_id", "device_type", "capabilities", "sensors", "protocol")


def _device_to_public(device: dict) -> dict:
    """Return the public DEVICE_PROTOCOL representation for one registered device."""
    return {
        "device_id": device["device_id"],
        "device_type": device["device_type"],
        "capabilities": device.get("capabilities", []),
        "sensors": device.get("sensors", []),
        "protocol": device.get("protocol", "custom"),
        "metadata": device.get("metadata", {}),
        "status": device.get("status", "online"),
        "last_heartbeat": device.get("last_heartbeat"),
        "state": device.get("state", {}),
        "latest_sensor": device.get("latest_sensor"),
    }


def _get_bearer_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


def _check_device_token(device_id: str) -> tuple[dict | None, tuple | None]:
    """Validate Authorization: Bearer token for a device route."""
    token = _get_bearer_token()
    with state._device_lock:
        device = state.devices.get(device_id)
        expected = state.device_tokens.get(device_id)
    if not device:
        return None, (jsonify({"ok": False, "error": f"设备 {device_id} 未注册", "code": "DEVICE_NOT_FOUND"}), 404)
    if not token or token != expected:
        return None, (jsonify({"ok": False, "error": "Token 无效或缺失", "code": "INVALID_TOKEN"}), 401)
    return device, None


def _sync_device_to_world(device: dict) -> None:
    """Expose protocol devices in the WorldModel so the regular UI can render them."""
    if not state.world_model:
        return
    dev_state = device.get("state", {}) or {}
    position = dev_state.get("position") or {}
    pos = [
        float(position.get("north", 0.0)),
        float(position.get("east", 0.0)),
        float(position.get("down", 0.0)),
    ]
    state.world_model.update_world_state({
        "robots": {
            device["device_id"]: {
                "robot_type": device.get("device_type", "CUSTOM"),
                "position": pos,
                "battery": dev_state.get("battery", 100.0),
                "status": dev_state.get("status", device.get("status", "online")),
                "in_air": dev_state.get("in_air", False),
                "armed": dev_state.get("armed", False),
                "sensor_status": {sensor: True for sensor in device.get("sensors", [])},
            }
        }
    })
    socketio.emit("world_state", state.get_world_snapshot())


@app.route("/api/device/register", methods=["POST"])
def api_device_register():
    """Register a generic DEVICE_PROTOCOL device and issue its bearer token."""
    data = request.get_json(silent=True) or {}
    missing = [field for field in _REQUIRED_DEVICE_FIELDS if not data.get(field)]
    if missing:
        return jsonify({"ok": False, "error": f"缺少必填字段: {', '.join(missing)}", "code": "MISSING_FIELDS"}), 400

    device_id = str(data["device_id"]).strip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id 不能为空", "code": "MISSING_FIELDS"}), 400

    now = time.time()
    token = f"aw_{device_id}_{secrets.token_hex(8)}"
    with state._device_lock:
        if device_id in state.devices:
            return jsonify({"ok": False, "error": f"设备 {device_id} 已注册", "code": "DEVICE_ALREADY_EXISTS"}), 409
        state.device_tokens[device_id] = token
        state.devices[device_id] = {
            "device_id": device_id,
            "device_type": data["device_type"],
            "capabilities": list(data.get("capabilities", [])),
            "sensors": list(data.get("sensors", [])),
            "protocol": data.get("protocol", "custom"),
            "metadata": data.get("metadata", {}),
            "status": "online",
            "last_heartbeat": now,
            "state": {"timestamp": now, "status": "idle", "battery": 100.0},
            "latest_sensor": None,
        }
        device = dict(state.devices[device_id])

    _sync_device_to_world(device)
    state.push_log("success", f"设备注册成功: {device_id}")
    return jsonify({"ok": True, "device_id": device_id, "token": token, "message": "设备注册成功"}), 201


@app.route("/api/device/<device_id>", methods=["DELETE"])
def api_device_delete(device_id):
    """Unregister a DEVICE_PROTOCOL device."""
    device, error = _check_device_token(device_id)
    if error:
        return error
    with state._device_lock:
        state.devices.pop(device_id, None)
        state.device_tokens.pop(device_id, None)
        state.device_sids.pop(device_id, None)
    if state.world_model:
        snapshot = state.world_model.get_world_state()
        robots = snapshot.get("robots", {})
        robots.pop(device_id, None)
        # WorldModel has no delete helper; replace robots through a direct update-compatible reset.
        state.world_model._state["robots"] = robots
        state.world_model._state["timestamp"] = time.time()
        socketio.emit("world_state", state.get_world_snapshot())
    state.push_log("info", f"设备已注销: {device_id}")
    return jsonify({"ok": True, "device_id": device_id, "message": "设备已注销"})


@app.route("/api/device/<device_id>/state", methods=["POST"])
def api_device_state(device_id):
    """Accept one-shot DEVICE_PROTOCOL state reports."""
    device, error = _check_device_token(device_id)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    now = float(payload.get("timestamp") or time.time())
    with state._device_lock:
        stored = state.devices[device_id]
        stored["last_heartbeat"] = now
        stored["status"] = "online"
        stored["state"] = {**stored.get("state", {}), **payload, "timestamp": now}
        device = dict(stored)
    _sync_device_to_world(device)
    socketio.emit("device_state", {"device_id": device_id, **device.get("state", {})})
    return jsonify({"ok": True, "device_id": device_id})


@app.route("/api/device/<device_id>/sensor", methods=["POST"])
def api_device_sensor(device_id):
    """Accept one-shot DEVICE_PROTOCOL sensor reports."""
    device, error = _check_device_token(device_id)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    sensor_type = payload.get("sensor_type")
    sensor_id = payload.get("sensor_id")
    if not sensor_type or not sensor_id:
        return jsonify({"ok": False, "error": "sensor_type 和 sensor_id 不能为空", "code": "MISSING_FIELDS"}), 400
    payload["timestamp"] = float(payload.get("timestamp") or time.time())
    with state._device_lock:
        stored = state.devices[device_id]
        stored["last_heartbeat"] = payload["timestamp"]
        stored["latest_sensor"] = payload
        device = dict(stored)
    socketio.emit("device_sensor", {"device_id": device_id, **payload})
    return jsonify({"ok": True, "device_id": device_id})


@app.route("/api/devices", methods=["GET"])
def api_devices():
    """List registered DEVICE_PROTOCOL devices."""
    with state._device_lock:
        devices = [_device_to_public(device) for device in state.devices.values()]
    return jsonify({"ok": True, "devices": devices, "count": len(devices)})


@app.route("/api/device/<device_id>/skills", methods=["GET"])
def api_device_skills(device_id):
    """Return a lightweight capability-to-skill mapping for device onboarding demos."""
    with state._device_lock:
        device = state.devices.get(device_id)
    if not device:
        return jsonify({"ok": False, "error": f"设备 {device_id} 未注册", "code": "DEVICE_NOT_FOUND"}), 404
    capabilities = set(device.get("capabilities", []))
    hard = []
    perception = []
    if "fly" in capabilities:
        hard.extend(["takeoff", "land", "fly_to", "hover"])
    if "drive" in capabilities:
        hard.extend(["move", "stop"])
    if "grab" in capabilities:
        hard.extend(["grab", "release"])
    if "camera" in capabilities:
        perception.extend(["observe", "detect_object"])
    if "lidar" in capabilities:
        perception.extend(["scan_area"])
    return jsonify({"ok": True, "device_id": device_id, "skills": {"hard": hard, "perception": perception, "soft": []}})


@app.route("/api/device/<device_id>/onboard", methods=["POST"])
def api_device_onboard(device_id):
    """Mark a registered demo device as onboarded for the lightweight client page."""
    device, error = _check_device_token(device_id)
    if error:
        return error
    with state._device_lock:
        state.devices[device_id].setdefault("metadata", {})["onboarded"] = True
    return jsonify({"ok": True, "device_id": device_id, "message": "设备档案已创建"})


@app.route("/api/device/<device_id>/action", methods=["POST"])
def api_device_action(device_id):
    """Send a DEVICE_PROTOCOL device_action event to an authenticated device socket."""
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    if not action:
        return jsonify({"ok": False, "error": "action 不能为空", "code": "MISSING_FIELDS"}), 400
    with state._device_lock:
        device = state.devices.get(device_id)
        sid = state.device_sids.get(device_id)
    if not device:
        return jsonify({"ok": False, "error": f"设备 {device_id} 未注册", "code": "DEVICE_NOT_FOUND"}), 404
    if not sid:
        return jsonify({"ok": False, "error": f"设备 {device_id} 离线", "code": "DEVICE_OFFLINE"}), 503
    action_payload = {
        "action_id": payload.get("action_id") or f"act_{int(time.time() * 1000)}",
        "device_id": device_id,
        "action": action,
        "params": payload.get("params", {}),
        "timeout": float(payload.get("timeout", 30.0)),
    }
    socketio.emit("device_action", action_payload, to=sid)
    return jsonify({"ok": True, **action_payload})


@app.route("/api/init", methods=["POST"])
def api_init():
    """初始化系统（异步，通过 WebSocket 推送进度）。"""
    if state.initialized:
        return jsonify({"ok": True, "msg": "系统已初始化"})
    t = threading.Thread(target=_do_init, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "初始化中，请监听 WebSocket log 事件"})


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(_get_system_status())


@app.route("/api/fleet", methods=["GET", "POST"])
def api_fleet():
    """Read or synchronize the active UAV fleet with AirSim settings."""
    if request.method == "GET":
        robots = (state.get_world_snapshot().get("robots") or {})
        fleet = [
            {
                "robot_id": robot_id,
                "position": robot.get("position", [0, 0, 0]),
                "vehicle": robot.get("airsim_vehicle"),
            }
            for robot_id, robot in sorted(robots.items(), key=lambda item: _vehicle_sort_key(item[0]))
            if str(robot_id).upper().startswith("UAV_")
        ]
        return jsonify({
            "ok": True,
            "count": len(fleet),
            "pool_size": _AIRSIM_POOL_SIZE,
            "ready_pool_size": int(getattr(state, "_airsim_pool_vehicle_count", 0)),
            "fleet": fleet,
            "syncing": _fleet_sync_lock.locked(),
        })

    if os.getenv("SIM_ADAPTER", "px4").lower() not in ("airsim", "airsim_physics"):
        return jsonify({"ok": False, "error": "Fleet synchronization requires the AirSim adapter"}), 409
    if state.is_executing:
        return jsonify({"ok": False, "error": "Cannot resize the fleet while a skill or mission is executing"}), 409
    if not _fleet_sync_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "AirSim fleet synchronization is already running"}), 409

    try:
        payload = request.get_json(silent=True) or {}
        count = max(1, min(int(payload.get("count", 1)), _AIRSIM_POOL_SIZE))
        positions = payload.get("fleet") or payload.get("positions") or []

        expected = [f"Drone_{index}" for index in range(1, _AIRSIM_POOL_SIZE + 1)]
        actual = []
        try:
            from adapters.airsim_rpc import AirSimDirectClient
            inventory_client = AirSimDirectClient(
                os.getenv("AIRSIM_HOST", "127.0.0.1"),
                int(os.getenv("AIRSIM_PORT", "41451")),
                timeout=3,
            )
            if inventory_client.connect() and inventory_client.ping():
                actual = sorted(
                    [str(name) for name in inventory_client.list_vehicles() if str(name).strip()],
                    key=_vehicle_sort_key,
                )
        except Exception:
            actual = []
        finally:
            if "inventory_client" in locals():
                inventory_client.close()

        from airsim_fleet import FleetSyncError, synchronize_airsim_fleet
        try:
            result = synchronize_airsim_fleet(
                count,
                positions,
                force_restart=bool(payload.get("force_restart")) or actual != expected,
                pool_size=_AIRSIM_POOL_SIZE,
            )
        except FleetSyncError as exc:
            logger.error("AirSim fleet synchronization failed: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

        from adapters.adapter_manager import get_adapter
        adapter = get_adapter()

        if result.get("restarted"):
            invalidate = getattr(adapter, "invalidate_connection", None) if adapter else None
            if callable(invalidate):
                invalidate()
            actual = []
            deadline = time.time() + 60
            while time.time() < deadline:
                client = None
                try:
                    from adapters.airsim_rpc import AirSimDirectClient
                    client = AirSimDirectClient(
                        os.getenv("AIRSIM_HOST", "127.0.0.1"),
                        int(os.getenv("AIRSIM_PORT", "41451")),
                        timeout=3,
                    )
                    if client.connect() and client.ping():
                        actual = sorted(
                            [str(name) for name in client.list_vehicles() if str(name).strip()],
                            key=_vehicle_sort_key,
                        )
                        if actual == expected:
                            break
                except Exception:
                    actual = []
                finally:
                    if client:
                        client.close()
                time.sleep(2)

        if actual != expected:
            return jsonify({
                **result,
                "ok": False,
                "error": "AirSim vehicle pool did not become ready",
                "expected_vehicles": expected,
                "actual_vehicles": actual,
            }), 504

        if adapter:
            connection = f"{os.getenv('AIRSIM_HOST', '127.0.0.1')}:{os.getenv('AIRSIM_PORT', '41451')}"
            with _adapter_reconnect_lock:
                connected = _adapter_connected(adapter) or adapter.connect(
                    connection_str=connection,
                    timeout=10,
                )
            if not connected:
                return jsonify({
                    **result,
                    "ok": False,
                    "error": "AirSim pool is ready but the backend adapter could not connect",
                    "actual_vehicles": actual,
                }), 502

            apply_layout = getattr(adapter, "apply_vehicle_pool_layout", None)
            if not callable(apply_layout):
                return jsonify({
                    **result,
                    "ok": False,
                    "error": "The active AirSim adapter does not support pooled fleet layouts",
                }), 501
            layout_result = apply_layout(result.get("pool") or [])
            if not layout_result.success:
                return jsonify({
                    **result,
                    "ok": False,
                    "error": f"AirSim pool positioning failed: {layout_result.message}",
                }), 502
            result["activation"] = layout_result.data

        state._desired_airsim_fleet_count = count
        try:
            _persist_fleet_count(count)
        except OSError as exc:
            logger.warning("Could not persist active AirSim fleet count: %s", exc)
        if adapter:
            _sync_airsim_fleet_to_world(adapter)

        result["actual_vehicles"] = actual
        result["active_count"] = count
        socketio.emit("world_state", state.get_world_snapshot())
        socketio.emit("skill_catalog", _get_skill_catalog())
        socketio.emit("system_status", _get_system_status())
        state.push_log(
            "success",
            f"AirSim active fleet synchronized: {count}/{_AIRSIM_POOL_SIZE} UAV(s); "
            f"restart={'yes' if result.get('restarted') else 'no'}",
        )
        return jsonify(result)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"Invalid fleet request: {exc}"}), 400
    finally:
        _fleet_sync_lock.release()


@app.route("/api/adapter/status", methods=["GET"])
def api_adapter_status():
    """Return the active control adapter status.

    This endpoint is intentionally separate from Gazebo sensor status: camera/LiDAR
    frames can be healthy while the flight-control adapter is disconnected. The UI
    and quickstart use this to avoid mistaking mock control for real PX4 control.
    """
    try:
        from adapters.adapter_manager import get_adapter
        adapter = get_adapter()
        if adapter is None:
            return jsonify({"ok": False, "adapter": None, "connected": False, "error": "adapter not initialized"})
        payload = {
            "ok": _adapter_connected(adapter),
            "adapter": getattr(adapter, "name", adapter.__class__.__name__),
            "description": getattr(adapter, "description", ""),
            "connected": _adapter_connected(adapter),
        }
        try:
            st = adapter.get_state()
            if st is None:
                payload["state"] = None
            else:
                payload["state"] = {
                    "armed": bool(st.armed),
                    "in_air": bool(st.in_air),
                    "mode": st.mode,
                    "position_ned": [st.position_ned.north, st.position_ned.east, st.position_ned.down] if st.position_ned else None,
                    "battery_percent": st.battery_percent,
                }
        except Exception as e:
            payload["state_error"] = str(e)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"ok": False, "connected": False, "error": str(e)}), 500


@app.route("/api/world", methods=["GET"])
def api_world():
    return jsonify(state.get_world_snapshot())


@app.route("/api/skills", methods=["GET"])
def api_skills():
    """返回全部机器人技能表 {robot_id: [skills]}，或指定机器人 ?robot=UAV_1。"""
    robot_id = request.args.get("robot")
    return jsonify(_get_skill_catalog(robot_id))


# ── 软技能管理 API ────────────────────────────────────────────────────────────

@app.route("/api/skills/soft", methods=["GET"])
def api_soft_skills():
    """返回所有软技能列表和摘要。"""
    from skills.soft_skill_manager import get_soft_skill_manager
    mgr = get_soft_skill_manager()
    skills = []
    for name in mgr.list_skills():
        info = mgr._cache.get(name, {})
        skills.append({
            "name": name,
            "title": info.get("title", name),
            "summary": info.get("summary", ""),
            "path": info.get("path", ""),
        })
    return jsonify({"ok": True, "skills": skills, "count": len(skills)})


@app.route("/api/skills/soft/<name>", methods=["GET"])
def api_soft_skill_detail(name):
    """获取单个软技能的完整文档。"""
    from skills.soft_skill_manager import get_soft_skill_manager
    mgr = get_soft_skill_manager()
    doc = mgr.get_skill_doc(name)
    if not doc:
        return jsonify({"ok": False, "msg": f"软技能 '{name}' 不存在"}), 404
    return jsonify({"ok": True, "name": name, "content": doc})


@app.route("/api/skills/soft", methods=["POST"])
def api_create_soft_skill():
    """手动创建软技能文档。body: {"name": str, "content": str}"""
    from skills.soft_skill_manager import get_soft_skill_manager
    mgr = get_soft_skill_manager()
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    content = data.get("content", "").strip()
    if not name or not content:
        return jsonify({"ok": False, "msg": "name 和 content 不能为空"}), 400
    if mgr.skill_exists(name):
        return jsonify({"ok": False, "msg": f"软技能 '{name}' 已存在"}), 409
    path = mgr.create_skill(name, content)
    return jsonify({"ok": True, "name": name, "path": path})


@app.route("/api/skills/soft/<name>", methods=["DELETE"])
def api_delete_soft_skill(name):
    """删除(淘汰)软技能。"""
    from skills.soft_skill_manager import get_soft_skill_manager
    mgr = get_soft_skill_manager()
    if not mgr.skill_exists(name):
        return jsonify({"ok": False, "msg": f"软技能 '{name}' 不存在"}), 404
    mgr.remove_skill(name)
    return jsonify({"ok": True, "name": name, "msg": "已淘汰"})


@app.route("/api/skills/soft/patterns", methods=["GET"])
def api_soft_skill_patterns():
    """检测重复模式, 返回可能生成新软技能的候选模式。"""
    from skills.dynamic_skill_gen import detect_patterns
    from memory.task_log import TaskLogger
    tl = TaskLogger()
    logs = tl.get_all_logs()
    min_count = int(request.args.get("min_count", 3))
    patterns = detect_patterns(logs, min_count=min_count)
    return jsonify({"ok": True, "patterns": patterns, "total_logs": len(logs)})


@app.route("/api/skills/soft/generate", methods=["POST"])
def api_generate_soft_skill():
    """
    根据指定的模式自动生成软技能文档。
    body: {"pattern": {"pattern": [...], "count": N, ...}}
    """
    from skills.soft_skill_manager import get_soft_skill_manager
    from skills.dynamic_skill_gen import generate_soft_skill_doc
    from llm_client import get_client

    mgr = get_soft_skill_manager()
    data = request.get_json() or {}
    pattern = data.get("pattern")
    if not pattern:
        return jsonify({"ok": False, "msg": "缺少 pattern 字段"}), 400

    try:
        client = get_client(module="doc_generator")
        result = generate_soft_skill_doc(
            pattern=pattern,
            llm_client=client,
            existing_skills=mgr.list_skills(),
        )
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

    if result is None:
        return jsonify({"ok": False, "msg": "LLM 建议跳过或生成失败"})

    path = mgr.create_skill(result["name"], result["content"])
    return jsonify({"ok": True, "name": result["name"], "path": path})


@app.route("/api/skills/soft/retire", methods=["POST"])
def api_retire_soft_skills():
    """
    检查并淘汰不合格的软技能。
    body: {"dry_run": true/false}  默认 dry_run=true
    """
    from skills.soft_skill_manager import get_soft_skill_manager
    from skills.dynamic_skill_gen import get_retirement_candidates, retire_skills

    mgr = get_soft_skill_manager()
    data = request.get_json() or {}
    dry_run = data.get("dry_run", True)

    # 尝试获取 skill_evolution (可能不存在)
    skill_evolution = None
    if state.runtime and hasattr(state.runtime, '_skill_evolution'):
        skill_evolution = state.runtime._skill_evolution

    candidates = get_retirement_candidates(mgr, skill_evolution)
    if dry_run:
        return jsonify({"ok": True, "dry_run": True, "candidates": candidates})
    else:
        retired = retire_skills(mgr, candidates, dry_run=False)
        return jsonify({"ok": True, "dry_run": False, "retired": retired, "count": len(retired)})


# ── 模型配置 API ──────────────────────────────────────────────────────────────

@app.route("/api/llm/config", methods=["GET"])
def api_llm_config():
    """
    返回当前 LLM 配置: 所有 provider、激活的 provider、各模块配置。
    API key 只返回掩码版本。
    """
    import config as cfg

    def _mask_key(key):
        if not key or len(key) < 8:
            return "***"
        return key[:4] + "..." + key[-4:]

    providers = {}
    for name, p in cfg.PROVIDERS.items():
        providers[name] = {
            "api_type": p.get("api_type", "openai_compat"),
            "base_url": p.get("base_url", ""),
            "api_key_masked": _mask_key(p.get("api_key", "")),
            "default_model": p.get("default_model", ""),
            "timeout": p.get("timeout", 60),
        }

    modules = {}
    for mod, mc in cfg.MODULE_CONFIG.items():
        resolved_provider = mc.get("provider") or cfg.ACTIVE_PROVIDER
        resolved_model = mc.get("model") or cfg.PROVIDERS.get(resolved_provider, {}).get("default_model", "")
        modules[mod] = {
            "provider": mc.get("provider"),
            "model": mc.get("model"),
            "resolved_provider": resolved_provider,
            "resolved_model": resolved_model,
        }

    return jsonify({
        "ok": True,
        "active_provider": cfg.ACTIVE_PROVIDER,
        "providers": providers,
        "modules": modules,
    })


@app.route("/api/llm/active", methods=["PUT"])
def api_set_active_provider():
    """切换全局激活 provider。body: {"provider": "ollama_local"}"""
    import config as cfg
    data = request.get_json() or {}
    provider = data.get("provider", "").strip()
    if not provider:
        return jsonify({"ok": False, "msg": "provider 不能为空"}), 400
    if provider not in cfg.PROVIDERS:
        return jsonify({"ok": False, "msg": f"未知 provider: {provider}"}), 404
    cfg.ACTIVE_PROVIDER = provider
    from llm_config_store import save_runtime_config
    save_runtime_config(cfg)
    state.push_log("info", f"全局 LLM 已切换到: {provider} ({cfg.PROVIDERS[provider]['default_model']})")
    return jsonify({"ok": True, "active_provider": provider})


@app.route("/api/llm/module/<module_name>", methods=["PUT"])
def api_set_module_config(module_name):
    """
    设置模块级 LLM 配置。
    body: {"provider": "openai", "model": "gpt-4o"}
    provider/model 设为 null 表示跟随全局。
    """
    import config as cfg
    if module_name not in cfg.MODULE_CONFIG:
        return jsonify({"ok": False, "msg": f"未知模块: {module_name}"}), 404
    data = request.get_json() or {}
    if "provider" in data:
        p = data["provider"]
        if p is not None and p not in cfg.PROVIDERS:
            return jsonify({"ok": False, "msg": f"未知 provider: {p}"}), 400
        cfg.MODULE_CONFIG[module_name]["provider"] = p
    if "model" in data:
        cfg.MODULE_CONFIG[module_name]["model"] = data["model"] or None
    from llm_config_store import save_runtime_config
    save_runtime_config(cfg)
    resolved_p = cfg.MODULE_CONFIG[module_name].get("provider") or cfg.ACTIVE_PROVIDER
    resolved_m = cfg.MODULE_CONFIG[module_name].get("model") or cfg.PROVIDERS.get(resolved_p, {}).get("default_model", "")
    state.push_log("info", f"模块 {module_name} LLM 配置更新: {resolved_p}/{resolved_m}")
    return jsonify({"ok": True, "module": module_name, "resolved_provider": resolved_p, "resolved_model": resolved_m})


@app.route("/api/llm/provider", methods=["POST"])
def api_add_provider():
    """
    新增或更新一个 provider。
    body: {
      "name": "my_provider",
      "base_url": "https://...",
      "api_key": "sk-...",
      "default_model": "gpt-4o",
      "timeout": 60
    }
    """
    import config as cfg
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    base_url = data.get("base_url", "").strip()
    api_key = data.get("api_key", "").strip()
    default_model = data.get("default_model", "").strip()
    timeout = data.get("timeout", 60)

    try:
        from llm_config_store import validate_provider_name
        name = validate_provider_name(name)
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    if not base_url:
        return jsonify({"ok": False, "msg": "base_url 不能为空"}), 400
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return jsonify({"ok": False, "msg": "base_url 必须以 http:// 或 https:// 开头"}), 400
    if not default_model:
        return jsonify({"ok": False, "msg": "default_model 不能为空"}), 400

    is_new = name not in cfg.PROVIDERS
    cfg.PROVIDERS[name] = {
        "api_type": "openai_compat",
        "base_url": base_url.rstrip("/"),
        "api_key": api_key or "none",
        "default_model": default_model,
        "timeout": int(timeout),
    }
    from llm_config_store import save_runtime_config
    save_runtime_config(cfg)
    action = "新增" if is_new else "更新"
    state.push_log("success", f"{action} LLM 渠道: {name} ({default_model} @ {base_url})")
    return jsonify({"ok": True, "action": action, "name": name})


@app.route("/api/llm/provider/<name>", methods=["DELETE"])
def api_delete_provider(name):
    """删除一个 provider (不能删除当前激活的)。"""
    import config as cfg
    if name not in cfg.PROVIDERS:
        return jsonify({"ok": False, "msg": f"provider '{name}' 不存在"}), 404
    if name == cfg.ACTIVE_PROVIDER:
        return jsonify({"ok": False, "msg": "不能删除当前激活的 provider"}), 400
    del cfg.PROVIDERS[name]
    from llm_config_store import save_runtime_config
    save_runtime_config(cfg)
    state.push_log("info", f"已删除 LLM 渠道: {name}")
    return jsonify({"ok": True, "name": name})


@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    """切换 manual / ai 模式。"""
    data = request.get_json() or {}
    new_mode = data.get("mode", "manual")
    if new_mode not in ("manual", "ai"):
        return jsonify({"ok": False, "msg": "mode 必须是 manual 或 ai"}), 400

    if new_mode == state.mode:
        return jsonify({"ok": True, "msg": f"已是 {new_mode} 模式"})

    # 如果从 AI → Manual，停止 AI 线程
    if state.mode == "ai" and new_mode == "manual":
        state._ai_stop_event.set()
        state.push_log("info", "已切换到手动模式，AI 规划已停止")

    state.mode = new_mode
    socketio.emit("system_status", _get_system_status())

    if new_mode == "ai":
        state.push_log("info", "已切换到 AI 模式，等待任务指令")

    return jsonify({"ok": True, "mode": state.mode})


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """返回最近的日志缓冲。"""
    with state._log_lock:
        return jsonify(state.log_buffer[-100:])


@app.route("/api/sensor/status", methods=["GET"])
def api_sensor_status():
    """返回传感器桥接状态。"""
    if state.sensor_bridge:
        return jsonify(state.sensor_bridge.get_status())
    return jsonify({"running": False, "error": "传感器桥接未启动"})


def _relay_channel_or_404(channel: str):
    normalized = str(channel or "").strip().lower()
    return normalized if normalized in AIRSIM_RELAY_CHANNELS else None


def _relay_snapshot_response(channel: str):
    try:
        upstream = requests.get(
            f"{AIRSIM_CAMERA_RELAY_URL}/snapshot/{channel}.jpg",
            timeout=(3.0, 8.0),
        )
    except requests.RequestException as exc:
        logger.warning("AirSim camera relay snapshot failed: %s", exc)
        return Response("AirSim camera relay unavailable", status=502)
    return Response(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "no-store"},
    )


def _relay_stream_response(channel: str):
    try:
        upstream = requests.get(
            f"{AIRSIM_CAMERA_RELAY_URL}/stream/{channel}",
            stream=True,
            timeout=(3.0, None),
        )
        upstream.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("AirSim camera relay stream failed: %s", exc)
        return Response("AirSim camera relay unavailable", status=502)

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        content_type=upstream.headers.get(
            "Content-Type",
            "multipart/x-mixed-replace; boundary=frame",
        ),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/sensor/relay/status", methods=["GET"])
def api_sensor_relay_status():
    if not AIRSIM_CAMERA_RELAY_ENABLED:
        return jsonify(ok=False, enabled=False), 503
    try:
        upstream = requests.get(
            f"{AIRSIM_CAMERA_RELAY_URL}/health",
            timeout=(2.0, 3.0),
        )
        payload = upstream.json()
        payload["enabled"] = True
        payload["relay_url"] = AIRSIM_CAMERA_RELAY_URL
        return jsonify(payload), upstream.status_code
    except (requests.RequestException, ValueError) as exc:
        return jsonify(ok=False, enabled=True, error=str(exc)), 502


@app.route("/api/sensor/relay/snapshot/<channel>", methods=["GET"])
def api_sensor_relay_snapshot(channel: str):
    normalized = _relay_channel_or_404(channel)
    if not normalized:
        return jsonify(error="unknown camera channel"), 404
    return _relay_snapshot_response(normalized)


@app.route("/api/sensor/relay/stream/<channel>", methods=["GET"])
def api_sensor_relay_stream(channel: str):
    normalized = _relay_channel_or_404(channel)
    if not normalized:
        return jsonify(error="unknown camera channel"), 404
    return _relay_stream_response(normalized)


@app.route("/api/sensor/camera", methods=["GET"])
def api_sensor_camera():
    """Return a JPEG snapshot for the requested camera direction."""
    import cv2
    import base64

    view = (request.args.get("view") or request.args.get("direction") or "front").lower()
    explicit_camera = request.args.get("camera") or request.args.get("camera_name")
    robot_id = request.args.get("robot_id")

    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    vehicle_for_robot = getattr(adapter, "vehicle_for_robot", None) if adapter else None
    vehicle = vehicle_for_robot(robot_id) if callable(vehicle_for_robot) else getattr(adapter, "_vehicle_name", "")
    relay_vehicle = vehicle_for_robot("UAV_1") if callable(vehicle_for_robot) else "Drone_1"
    use_relay = (
        AIRSIM_CAMERA_RELAY_ENABLED
        and view in AIRSIM_RELAY_CHANNELS
        and (not robot_id or vehicle == relay_vehicle)
    )

    if use_relay:
        return _relay_snapshot_response(view)

    if state.sensor_bridge:
        img = state.sensor_bridge.get_camera_image()
        if img is not None:
            _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            return Response(buf.tobytes(), mimetype="image/jpeg")

    cached = _get_cached_airsim_camera(view) if not robot_id or vehicle == relay_vehicle else None
    cached_frame = _jpeg_bytes_from_payload(cached)
    if cached_frame:
        return Response(cached_frame, mimetype="image/jpeg")

    client = getattr(adapter, "_client", None) if adapter else None
    if client:
        candidates = [explicit_camera] if explicit_camera else _camera_candidates(view)
        for camera_name in candidates:
            if not camera_name:
                continue
            try:
                payload = _airsim_fetch_image(
                    client,
                    camera_name,
                    vehicle_name=vehicle,
                    jpeg_quality=92,
                    max_width=1920,
                    max_height=1080,
                )
            except Exception as exc:
                logger.debug("camera snapshot candidate %s failed: %s", camera_name, exc)
                continue
            frame = _jpeg_bytes_from_payload(payload)
            if frame:
                _RESOLVED_AIRSIM_CAMERAS[view] = str(camera_name)
                return Response(frame, mimetype="image/jpeg")

    if adapter and hasattr(adapter, 'get_image_base64'):
        candidates = [explicit_camera] if explicit_camera else _camera_candidates(view)
        for camera_name in candidates:
            if not camera_name:
                continue
            b64 = adapter.get_image_base64(camera_name)
            if b64:
                _RESOLVED_AIRSIM_CAMERAS[view] = str(camera_name)
                return Response(base64.b64decode(b64), mimetype="image/jpeg")
    return Response("No camera available", status=503)


@app.route("/api/sensor/camera/stream", methods=["GET"])
def api_sensor_camera_stream():
    """Stream the requested camera as MJPEG. Browsers decode this with less React churn."""
    view = (request.args.get("view") or request.args.get("direction") or "front").lower()
    explicit_camera = request.args.get("camera") or request.args.get("camera_name")
    robot_id = request.args.get("robot_id")

    fps = _clamped_int(request.args.get("fps"), int(os.getenv("AIRSIM_MJPEG_FPS", "8")), 1, 20)
    quality = _clamped_int(request.args.get("quality"), int(os.getenv("AIRSIM_MJPEG_QUALITY", "75")), 35, 95)
    max_width = _clamped_int(request.args.get("max_width"), int(os.getenv("AIRSIM_MJPEG_MAX_WIDTH", "960")), 160, 2560)
    max_height = _clamped_int(request.args.get("max_height"), int(os.getenv("AIRSIM_MJPEG_MAX_HEIGHT", "540")), 120, 1440)

    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    vehicle_for_robot = getattr(adapter, "vehicle_for_robot", None) if adapter else None
    vehicle = vehicle_for_robot(robot_id) if callable(vehicle_for_robot) else getattr(adapter, "_vehicle_name", "")
    relay_vehicle = vehicle_for_robot("UAV_1") if callable(vehicle_for_robot) else "Drone_1"
    use_relay = (
        AIRSIM_CAMERA_RELAY_ENABLED
        and view in AIRSIM_RELAY_CHANNELS
        and (not robot_id or vehicle == relay_vehicle)
    )
    if use_relay:
        return _relay_stream_response(view)

    rpc_client = None

    def _get_rpc_client():
        nonlocal rpc_client, adapter, vehicle
        if rpc_client:
            return rpc_client
        adapter = get_adapter()
        vehicle_for_robot = getattr(adapter, "vehicle_for_robot", None) if adapter else None
        vehicle = vehicle_for_robot(robot_id) if callable(vehicle_for_robot) else getattr(adapter, "_vehicle_name", "")
        if adapter:
            try:
                from adapters.airsim_rpc import AirSimDirectClient
                ip = getattr(adapter, '_airsim_host', os.getenv('AIRSIM_HOST', '127.0.0.1'))
                port = int(getattr(adapter, '_airsim_port', os.getenv('AIRSIM_PORT', '41451')))
                client = AirSimDirectClient(ip, port, timeout=5)
                if client.connect():
                    rpc_client = client
                    return rpc_client
            except Exception as exc:
                logger.debug("camera MJPEG direct RPC unavailable: %s", exc)
            rpc_client = getattr(adapter, '_client', None)
        return rpc_client

    def _frame_getter():
        cached = _get_cached_airsim_camera(view) if not robot_id or vehicle == relay_vehicle else None
        cached_frame = _jpeg_bytes_from_payload(cached)
        if cached_frame:
            return cached_frame

        if state.sensor_bridge:
            try:
                import cv2
                img = state.sensor_bridge.get_camera_image(view)
                if img is not None:
                    h, w = img.shape[:2]
                    scale = min(float(max_width) / float(w), float(max_height) / float(h), 1.0)
                    if scale < 1.0:
                        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
                    if ok:
                        return buf.tobytes()
            except Exception as exc:
                logger.debug("sensor bridge MJPEG frame failed: %s", exc)

        client = _get_rpc_client()
        if not client:
            return None
        candidates = [explicit_camera] if explicit_camera else _camera_candidates(view)
        for camera_name in candidates:
            if not camera_name:
                continue
            try:
                payload = _airsim_fetch_image(
                    client,
                    camera_name,
                    vehicle_name=vehicle,
                    jpeg_quality=quality,
                    max_width=max_width,
                    max_height=max_height,
                )
            except Exception as exc:
                logger.debug("camera MJPEG candidate %s failed: %s", camera_name, exc)
                continue
            frame = _jpeg_bytes_from_payload(payload)
            if frame:
                _RESOLVED_AIRSIM_CAMERAS[view] = str(camera_name)
                return frame
        return None

    return _mjpeg_response(_frame_getter, fps=fps, name=f"camera:{view}")


@app.route("/api/pixel-streaming/view", methods=["POST"])
def api_pixel_streaming_view():
    """Bind Unreal's streamed viewport to one UAV-mounted AirSim camera."""
    payload = request.get_json(silent=True) or {}
    robot_id = str(payload.get("robot_id") or state.current_robot or "UAV_1")
    view = str(payload.get("view") or "front").lower()
    if view not in AIRSIM_CAMERA_CANDIDATES:
        return jsonify({"ok": False, "error": f"unknown camera view: {view}"}), 400

    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    client = getattr(adapter, "_client", None) if adapter else None
    vehicle_for_robot = getattr(adapter, "vehicle_for_robot", None) if adapter else None
    if not client or not callable(vehicle_for_robot):
        return jsonify({"ok": False, "error": "AirSim adapter unavailable"}), 503

    vehicle = vehicle_for_robot(robot_id)
    origin = getattr(adapter, "_vehicle_spawn_poses", {}).get(vehicle, (0.0, 0.0, 0.0))
    last_error = None
    for camera_name in _camera_candidates(view):
        if not camera_name:
            continue
        try:
            info = client.sim_get_camera_info(camera_name, vehicle, False)
            local_pose = (info or {}).get("pose") or {}
            local_position = local_pose.get("position") or {}
            orientation = local_pose.get("orientation") or {}
            if not local_position or not orientation:
                continue
            global_pose = {
                "position": {
                    "x_val": float(origin[0]) + float(local_position.get("x_val", 0.0)),
                    "y_val": float(origin[1]) + float(local_position.get("y_val", 0.0)),
                    "z_val": float(origin[2]) + float(local_position.get("z_val", 0.0)),
                },
                "orientation": {
                    "w_val": float(orientation.get("w_val", 1.0)),
                    "x_val": float(orientation.get("x_val", 0.0)),
                    "y_val": float(orientation.get("y_val", 0.0)),
                    "z_val": float(orientation.get("z_val", 0.0)),
                },
            }
            if not client.sim_set_object_pose("ExternalCamera", global_pose, True):
                raise RuntimeError("AirSim rejected ExternalCamera pose")
            logger.info(
                "Pixel Streaming viewport: %s -> %s/%s at (%.2f, %.2f, %.2f)",
                robot_id,
                vehicle,
                view,
                global_pose["position"]["x_val"],
                global_pose["position"]["y_val"],
                global_pose["position"]["z_val"],
            )
            return jsonify({
                "ok": True,
                "robot_id": robot_id,
                "vehicle": vehicle,
                "view": view,
                "camera": camera_name,
                "pose": global_pose,
            })
        except Exception as exc:
            last_error = exc
    return jsonify({"ok": False, "error": str(last_error or "camera pose unavailable")}), 502


@app.route("/api/sensor/scene", methods=["GET"])
def api_sensor_scene():
    """Return a JPEG snapshot from an AirSim external/global scene camera."""
    if AIRSIM_CAMERA_RELAY_ENABLED:
        return _relay_snapshot_response("scene")

    from adapters.adapter_manager import get_adapter
    quality = _clamped_int(request.args.get("quality"), int(os.getenv("AIRSIM_SCENE_JPEG_QUALITY", "80")), 35, 95)
    max_width = _clamped_int(request.args.get("max_width"), int(os.getenv("AIRSIM_SCENE_MAX_WIDTH", "1280")), 160, 3840)
    max_height = _clamped_int(request.args.get("max_height"), int(os.getenv("AIRSIM_SCENE_MAX_HEIGHT", "720")), 120, 2160)

    cached = _get_cached_airsim_scene()
    cached_frame = _jpeg_bytes_from_payload(cached)
    if cached_frame:
        return Response(cached_frame, mimetype="image/jpeg")

    adapter = get_adapter()
    rpc_client = getattr(adapter, '_client', None) if adapter else None
    if not rpc_client:
        return Response("No AirSim client available", status=503)

    scene_payload = _fetch_airsim_scene_camera(
        rpc_client,
        jpeg_quality=quality,
        max_width=max_width,
        max_height=max_height,
    )
    if not scene_payload:
        return Response("No AirSim global scene camera available", status=503)
    return Response(base64.b64decode(scene_payload["image"]), mimetype="image/jpeg")


@app.route("/api/sensor/scene/stream", methods=["GET"])
def api_sensor_scene_stream():
    """Stream the AirSim global/external camera as MJPEG."""
    if AIRSIM_CAMERA_RELAY_ENABLED:
        return _relay_stream_response("scene")

    fps = _clamped_int(request.args.get("fps"), int(os.getenv("AIRSIM_SCENE_MJPEG_FPS", "2")), 1, 12)
    quality = _clamped_int(request.args.get("quality"), int(os.getenv("AIRSIM_SCENE_MJPEG_QUALITY", "75")), 35, 95)
    max_width = _clamped_int(request.args.get("max_width"), int(os.getenv("AIRSIM_SCENE_MJPEG_MAX_WIDTH", "1280")), 160, 3840)
    max_height = _clamped_int(request.args.get("max_height"), int(os.getenv("AIRSIM_SCENE_MJPEG_MAX_HEIGHT", "720")), 120, 2160)

    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    rpc_client = None

    def _get_rpc_client():
        nonlocal rpc_client, adapter
        if rpc_client:
            return rpc_client
        adapter = get_adapter()
        if adapter:
            try:
                from adapters.airsim_rpc import AirSimDirectClient
                ip = getattr(adapter, '_airsim_host', os.getenv('AIRSIM_HOST', '127.0.0.1'))
                port = int(getattr(adapter, '_airsim_port', os.getenv('AIRSIM_PORT', '41451')))
                client = AirSimDirectClient(ip, port, timeout=5)
                if client.connect():
                    rpc_client = client
                    return rpc_client
            except Exception as exc:
                logger.debug("scene MJPEG direct RPC unavailable: %s", exc)
            rpc_client = getattr(adapter, '_client', None)
        return rpc_client

    def _frame_getter():
        cached = _get_cached_airsim_scene()
        cached_frame = _jpeg_bytes_from_payload(cached)
        if cached_frame:
            return cached_frame

        client = _get_rpc_client()
        if not client:
            return None
        try:
            payload = _fetch_airsim_scene_camera(
                client,
                jpeg_quality=quality,
                max_width=max_width,
                max_height=max_height,
            )
            return _jpeg_bytes_from_payload(payload)
        except Exception as exc:
            logger.debug("scene MJPEG frame failed: %s", exc)
            return None

    return _mjpeg_response(_frame_getter, fps=fps, name="scene")





@app.route("/api/sensor/lidar", methods=["GET"])
def api_sensor_lidar():
    """Return a selected UAV's LiDAR scan or the active simulator bridge scan."""
    import math
    import struct

    robot_id = request.args.get("robot_id")
    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    client = getattr(adapter, "_client", None) if adapter else None
    vehicle_for_robot = getattr(adapter, "vehicle_for_robot", None) if adapter else None
    if client and callable(vehicle_for_robot):
        vehicle = vehicle_for_robot(robot_id)
        try:
            data = client.get_lidar_data("LidarSensor1", vehicle) or {}
            points = data.get("point_cloud") or []
            if isinstance(points, (bytes, bytearray)):
                points = struct.unpack(f"{len(points) // 4}f", points)
            max_range = 100.0
            ranges = [max_range] * 360
            for index in range(0, len(points) - 2, 3):
                x, y, z = map(float, points[index:index + 3])
                distance = math.sqrt(x * x + y * y)
                if distance <= 0.1 or distance > max_range:
                    continue
                angle = math.atan2(y, x)
                bin_index = int(((angle + math.pi) / (2 * math.pi)) * 360) % 360
                ranges[bin_index] = min(ranges[bin_index], round(distance, 2))
            return jsonify({
                "is_3d": True,
                "robot_id": robot_id or state.current_robot,
                "vehicle": vehicle,
                "ranges": ranges,
                "angle_min": -math.pi,
                "angle_max": math.pi,
                "angle_increment": 2 * math.pi / 360,
                "range_min": 0.1,
                "range_max": max_range,
                "count": len(points) // 3,
            })
        except Exception as exc:
            logger.debug("AirSim LiDAR read failed for %s: %s", vehicle, exc)

    if state.sensor_bridge:
        scan = state.sensor_bridge.get_lidar_scan()
        if scan is not None:
            rmax = scan["range_max"]
            scan["ranges"] = [
                r if (math.isfinite(r) and r >= scan["range_min"]) else rmax
                for r in scan["ranges"]
            ]
            return jsonify(scan)
    return jsonify({"error": "暂无雷达数据"}), 503


@app.route("/api/sensor/distance", methods=["GET"])
def api_sensor_distance():
    """Return the selected UAV's real bottom distance reading."""
    robot_id = request.args.get("robot_id") or state.current_robot or "UAV_1"
    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    vehicle_for_robot = getattr(adapter, "vehicle_for_robot", None) if adapter else None
    read_clearance = getattr(adapter, "get_ground_clearance", None) if adapter else None
    if not callable(vehicle_for_robot) or not callable(read_clearance):
        return jsonify({"ok": False, "error": "AirSim distance sensor unavailable"}), 503
    vehicle = vehicle_for_robot(robot_id)
    distance = read_clearance(vehicle)
    if distance is None:
        return jsonify({
            "ok": False,
            "robot_id": robot_id,
            "vehicle": vehicle,
            "sensor": "BottomDistance",
            "error": "No valid bottom distance reading",
        }), 503
    return jsonify({
        "ok": True,
        "robot_id": robot_id,
        "vehicle": vehicle,
        "sensor": "BottomDistance",
        "distance_m": round(distance, 3),
        "hover_target_m": 4.0,
    })


# ── 前端静态文件服务 ──────────────────────────────────────────────────────────

@app.route("/")
@app.route("/body")
def serve_body_sense_page():
    """首页 / BodySense 页面"""
    # 优先返回前端 SPA
    index = os.path.join(_UI_DIST, "index.html")
    if os.path.exists(index):
        return send_file(index)
    body_html = os.path.join(_BASE_DIR, "ui", "body.html")
    if os.path.exists(body_html):
        return send_file(body_html)
    return "<h2>前端未构建，请先运行 cd frontend && npm run build</h2>", 200


@app.route("/<path:path>")
def serve_frontend(path):
    """Serve React build dist. Non-API routes fall through to index.html (SPA)."""
    if path.startswith("api/") or path.startswith("socket.io"):
        return jsonify({"error": "not found"}), 404
    if path and os.path.exists(os.path.join(_UI_DIST, path)):
        return send_from_directory(_UI_DIST, path)
    index = os.path.join(_UI_DIST, "index.html")
    if os.path.exists(index):
        return send_file(index)
    return "<h2>前端未构建，请先运行 cd frontend && npm run build</h2>", 200


# ══════════════════════════════════════════════════════════════════════════════
#  WebSocket 事件
# ══════════════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def on_connect():
    logger.info("客户端连接: %s", request.sid)
    sid = request.sid

    def _send_initial_state():
        # Send initial events after the namespace connection is acknowledged.
        # Some Socket.IO clients reject event packets that arrive before the
        # connect ACK; browser clients are permissive, but the verifier uses
        # python-socketio and should exercise the same public endpoint reliably.
        time.sleep(0.05)
        socketio.emit("system_status", _get_system_status(), to=sid)
        socketio.emit("world_state", state.get_world_snapshot(), to=sid)
        if state.robot_registries:
            socketio.emit("skill_catalog", _get_skill_catalog(), to=sid)
        with state._log_lock:
            recent_logs = list(state.log_buffer[-50:])
        for entry in recent_logs:
            socketio.emit("log", entry, to=sid)

    socketio.start_background_task(_send_initial_state)


@socketio.on("disconnect")
def on_disconnect():
    logger.info("客户端断开: %s", request.sid)
    with state._device_lock:
        stale = [device_id for device_id, sid in state.device_sids.items() if sid == request.sid]
        for device_id in stale:
            state.device_sids.pop(device_id, None)


@socketio.on("device_connect")
def on_device_connect(data):
    """Authenticate a DEVICE_PROTOCOL Socket.IO device connection."""
    device_id = (data or {}).get("device_id")
    token = (data or {}).get("token")
    with state._device_lock:
        expected = state.device_tokens.get(device_id)
        device = state.devices.get(device_id)
        if device and expected and token == expected:
            state.device_sids[device_id] = request.sid
            device["status"] = "online"
            device["last_heartbeat"] = time.time()
            ok = True
        else:
            ok = False
    if not ok:
        emit("device_connected", {"ok": False, "device_id": device_id, "error": "Token 无效或设备未注册", "code": "INVALID_TOKEN"})
        return
    emit("device_connected", {"ok": True, "device_id": device_id, "message": "WebSocket 已认证"})
    state.push_log("info", f"设备 WebSocket 已认证: {device_id}")


@socketio.on("heartbeat")
def on_device_heartbeat(data):
    device_id = (data or {}).get("device_id")
    now = float((data or {}).get("timestamp") or time.time())
    with state._device_lock:
        device = state.devices.get(device_id)
        if device:
            device["last_heartbeat"] = now
            device["status"] = "online"
    emit("heartbeat_ack", {"device_id": device_id, "timestamp": time.time()})


@socketio.on("device_state")
def on_device_state(data):
    device_id = (data or {}).get("device_id")
    if not device_id:
        emit("device_state_ack", {"ok": False, "error": "device_id 不能为空", "code": "MISSING_FIELDS"})
        return
    now = float((data or {}).get("timestamp") or time.time())
    with state._device_lock:
        if device_id not in state.devices:
            emit("device_state_ack", {"ok": False, "device_id": device_id, "error": "设备未注册", "code": "DEVICE_NOT_FOUND"})
            return
        stored = state.devices[device_id]
        stored["last_heartbeat"] = now
        stored["status"] = "online"
        stored["state"] = {**stored.get("state", {}), **(data or {}), "timestamp": now}
        device = dict(stored)
    _sync_device_to_world(device)
    emit("device_state_ack", {"ok": True, "device_id": device_id})


@socketio.on("device_sensor")
def on_device_sensor(data):
    device_id = (data or {}).get("device_id")
    if not device_id:
        emit("device_sensor_ack", {"ok": False, "error": "device_id 不能为空", "code": "MISSING_FIELDS"})
        return
    payload = dict(data or {})
    payload["timestamp"] = float(payload.get("timestamp") or time.time())
    with state._device_lock:
        if device_id not in state.devices:
            emit("device_sensor_ack", {"ok": False, "device_id": device_id, "error": "设备未注册", "code": "DEVICE_NOT_FOUND"})
            return
        stored = state.devices[device_id]
        stored["last_heartbeat"] = payload["timestamp"]
        stored["latest_sensor"] = payload
    emit("device_sensor_ack", {"ok": True, "device_id": device_id})


@socketio.on("action_result")
def on_device_action_result(data):
    """Receive action execution result from a protocol device."""
    state.push_log("info", f"设备动作回报: {(data or {}).get('device_id')}:{(data or {}).get('action_id')}", {"device_action_result": data or {}})
    emit("action_result_ack", {"ok": True, "action_id": (data or {}).get("action_id")})


_SWARM_SKILL_NAMES = frozenset({
    "swarm_area_search",
    "swarm_rendezvous",
    "swarm_formation_hold",
    "swarm_orbit_hold",
})


def _execution_robot_ids(robot_id: str, skill_name: str, parameters: dict) -> list[str]:
    if skill_name not in _SWARM_SKILL_NAMES:
        return [robot_id]

    raw = parameters.get("robot_ids")
    if isinstance(raw, str):
        values = raw.replace(",", " ").replace(";", " ").split()
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []

    available = state.world_model.get_world_state().get("robots", {}) if state.world_model else {}
    if not values:
        values = [
            rid
            for rid, robot in available.items()
            if str(robot.get("robot_type", "UAV")).upper() == "UAV"
        ]
    normalized = []
    for value in values:
        candidate = str(value or "").strip().replace("-", "_")
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    if robot_id not in normalized:
        normalized.insert(0, robot_id)
    return sorted(normalized, key=_vehicle_sort_key)


@socketio.on("execute_skill")
def on_execute_skill(data):
    """
    手动模式：执行单个技能。
    data: {
        "robot_id": "UAV_1",
        "skill_name": "takeoff",
        "parameters": {"altitude": 5.0}
    }
    """
    if not state.initialized:
        emit("skill_result", {"ok": False, "error": "系统未初始化"})
        return

    if state.mode != "manual":
        emit("skill_result", {"ok": False, "error": "当前不是手动模式"})
        return

    if state.is_executing:
        emit("skill_result", {"ok": False, "error": "自主任务正在执行，请稍候"})
        return

    data = data or {}
    robot_id = data.get("robot_id", state.current_robot)
    skill_name = data.get("skill_name", "")
    parameters = dict(data.get("parameters", {}) or {})

    if not skill_name:
        emit("skill_result", {"ok": False, "error": "skill_name 不能为空"})
        return

    execution_robot_ids = _execution_robot_ids(robot_id, skill_name, parameters)
    available_robots = (
        state.world_model.get_world_state().get("robots", {})
        if state.world_model
        else {}
    )
    missing_robots = [
        reserved_robot
        for reserved_robot in execution_robot_ids
        if reserved_robot not in available_robots
    ]
    if missing_robots:
        emit("skill_result", {
            "ok": False,
            "error": f"未注册的无人机: {', '.join(missing_robots)}",
            "robot": robot_id,
            "robots": execution_robot_ids,
            "skill": skill_name,
            "code": "ROBOT_NOT_FOUND",
        })
        return

    reserved, busy_robots = state.try_begin_robot_executions(execution_robot_ids)
    if not reserved:
        emit("skill_result", {
            "ok": False,
            "error": f"{', '.join(busy_robots)} 正在执行技能，请等待完成",
            "robot": robot_id,
            "robots": execution_robot_ids,
            "skill": skill_name,
            "code": "ROBOT_BUSY",
        })
        return

    if skill_name in _SWARM_SKILL_NAMES:
        parameters["robot_ids"] = execution_robot_ids
    socketio.emit("system_status", _get_system_status())

    # 在后台线程执行，避免阻塞 SocketIO 事件循环
    def _run():
        state.push_log(
            "info",
            f"▶ 执行: [{', '.join(execution_robot_ids)}] {skill_name}",
            {"skill": skill_name, "robot": robot_id, "robots": execution_robot_ids},
        )

        try:
            result = state.runtime.dispatch_skill({
                "step": 1,
                "skill": skill_name,
                "robot": robot_id,
                "parameters": parameters,
            })

            ok = result.success
            level = "success" if ok else "error"
            state.push_log(level, f"{'✅' if ok else '❌'} {skill_name} → {'成功' if ok else '失败: ' + result.error_msg}",
                           {"skill": skill_name, "robot": robot_id, "output": result.output})

            # 回写该机器人的技能执行状态（per-robot 隔离）
            robot_reg = state.robot_registries.get(robot_id)
            if robot_reg:
                robot_reg.update_execution_status(skill_name, ok)

            result_payload = {
                "ok": ok,
                "skill": skill_name,
                "robot": robot_id,
                "robots": execution_robot_ids,
                "output": result.output,
                "error": result.error_msg,
                "cost_time": result.cost_time,
                "logs": result.logs,
            }
            socketio.emit("skill_result", result_payload)

            # 推送更新后的世界状态
            socketio.emit("world_state", state.get_world_snapshot())
            socketio.emit("skill_catalog", _get_skill_catalog())

        except Exception as e:
            logger.exception("技能执行异常")
            state.push_log("error", f"技能执行异常: {e}")
            socketio.emit("skill_result", {"ok": False, "error": str(e), "skill": skill_name, "robot": robot_id})
        finally:
            state.end_robot_executions(execution_robot_ids)
            socketio.emit("system_status", _get_system_status())

    t = threading.Thread(
        target=_run,
        daemon=True,
        name=f"skill-{skill_name}-{robot_id}",
    )
    try:
        t.start()
    except Exception:
        state.end_robot_executions(execution_robot_ids)
        socketio.emit("system_status", _get_system_status())
        raise


@socketio.on("select_robot")
def on_select_robot(data):
    """切换当前选中机器人。"""
    robot_id = data.get("robot_id", "UAV_1")
    state.current_robot = robot_id
    emit("system_status", _get_system_status())


@socketio.on("ai_task")
def on_ai_task(data):
    """
    AI 模式：提交自然语言任务，让 LLM 规划并执行。
    data: {"task": "搜索北部区域，发现目标后拍照记录", "use_tools": false}
    """
    if not state.initialized:
        emit("ai_plan_result", {"ok": False, "error": "系统未初始化"})
        return

    if state.mode != "ai":
        emit("ai_plan_result", {"ok": False, "error": "请先切换到 AI 模式"})
        return

    # ── Check LLM configuration before executing ──
    try:
        import config as cfg
        provider_name = cfg.ACTIVE_PROVIDER
        provider_cfg = cfg.PROVIDERS.get(provider_name, {})
        api_key = provider_cfg.get("api_key", "")
        if not api_key or api_key in ("", "your-llm-api-key-here", "your-key-here"):
            emit("ai_plan_result", {
                "ok": False,
                "error": f"LLM 未配置: 当前 provider [{provider_name}] 的 API Key 为空。请先在 .env 文件中配置 API Key，或通过界面右上角 ⚙️ 添加模型。"
            })
            return
    except Exception:
        pass

    if state.is_executing:
        # 执行中: 注入用户消息
        task = data.get("task", "")
        if task and state._current_agent_loop:
            state._current_agent_loop.inject_user_message(task)
            emit("ai_plan_result", {"ok": True, "injected": True, "message": f"已注入指令: {task[:50]}"})
        else:
            emit("ai_plan_result", {"ok": False, "error": "正在执行中，请稍候"})
        return

    task = data.get("task", "")
    use_tools = data.get("use_tools", False)

    if not task:
        emit("ai_plan_result", {"ok": False, "error": "任务描述不能为空"})
        return

    state._ai_stop_event.clear()

    def _run_ai():
        state.is_executing = True
        socketio.emit("system_status", _get_system_status())
        state.push_log("info", f"🤖 AI 任务: {task}")

        try:
            # ai_task 是明确的任务执行入口，直接启动自主 Agent 循环
            # 对话/查询类请求走 ai_chat 入口；ai_task 始终执行 AgentLoop
            from llm_client import get_client
            client = get_client(module="planner")

            import brain.planner_agent as planner

            # 使用 AgentLoop 自主执行任务
            socketio.emit("ai_thinking", {"phase": "planning", "detail": "正在启动自主 Agent..."})
            state.push_log("info", "🧠 启动 Agent 自主循环...")

            from brain.agent_loop import AgentLoop
            reg = state.robot_registries.get(state.current_robot)

            def on_thinking(iteration, output):
                if output is None:
                    output = {}
                thinking = output.get("thinking", "")
                decision = output.get("decision", "")
                action = output.get("action", {})
                progress = output.get("goal_progress", "")
                reflection = output.get("reflection")
                # 新事件：结构化思考链，前端展示每轮卡片
                socketio.emit("ai_thought", {
                    "iteration": iteration,
                    "thinking": thinking,
                    "decision": decision,
                    "reflection": reflection,
                    "progress": progress,
                    "skill": action.get("skill", ""),
                    "parameters": action.get("parameters", {}),
                })
                socketio.emit("ai_thinking", {
                    "phase": "thinking",
                    "detail": f"[第{iteration}轮] {thinking}",
                    "iteration": iteration,
                    "decision": decision,
                    "action": action,
                    "progress": progress,
                })
                state.push_log("info", f"🧠 第{iteration}轮: {thinking}")

                # 同时把 thinking 作为清洁文本推到 stream (而不是原始 JSON token)
                socketio.emit("ai_stream", {"token": "", "done": True})  # 清空上一轮
                clean_text = f"[第{iteration}轮] {thinking}"
                if progress:
                    clean_text += f"\n进度: {progress}"
                socketio.emit("ai_stream", {"token": clean_text, "done": False})

            def on_action(iteration, skill, params, result):
                status = "✅" if result.success else "❌"
                state.push_log(
                    "success" if result.success else "error",
                    f"  {status} {skill} ({result.cost_time:.1f}s)" + (f" - {result.error_msg}" if not result.success else ""),
                )
                socketio.emit("world_state", state.get_world_snapshot())

            final_result = {"success": False, "summary": ""}
            def on_complete(success, summary):
                final_result["success"] = success
                final_result["summary"] = summary
                socketio.emit("ai_thinking", {"phase": "idle", "detail": ""})
                # 把完成报告发到聊天框
                status_icon = "✅" if success else "❌"
                socketio.emit("ai_chat_reply", {
                    "ok": True,
                    "reply": f"{status_icon} 任务{'完成' if success else '未完成'}\n\n{summary}",
                    "intent": "task_report",
                })

            # LLM streaming 回调 — 禁用原始 token 推送, thinking 内容由 on_thinking 以清洁文本推送
            def _on_stream(token):
                pass  # 不再推送碎片 JSON token

            loop = AgentLoop(
                goal=task,
                llm_client=client,
                runtime=state.runtime,
                world_model=state.world_model,
                skill_registry=reg,
                max_iterations=50,
                on_thinking=on_thinking,
                on_action=on_action,
                on_complete=on_complete,
                on_stream=_on_stream,
                stop_event=state._ai_stop_event,
                experience_store=getattr(state, "experience_store", None),
            )
            # 注入被动感知引擎引用
            try:
                from skills.perception_skills import _get_passive_perception
                loop._passive_engine = _get_passive_perception()
            except Exception:
                loop._passive_engine = None
            state._current_agent_loop = loop
            loop.run()
            state._current_agent_loop = None
            # 通知 streaming 结束
            socketio.emit("ai_stream", {"token": "", "done": True})

            # 推送执行报告
            summary = loop.get_summary()
            ok = final_result["success"]
            status_str = "✅ 目标达成" if ok else "❌ 任务未完成"
            state.push_log("success" if ok else "error",
                f"{status_str} | {summary['successful']}/{summary['total_actions']} 步成功 | {summary['iterations']} 轮思考")

            socketio.emit("ai_execution_report", {
                "ok": ok,
                "task": task,
                "completed_steps": summary["successful"],
                "total_steps": summary["total_actions"],
                "replans": 0,
                "cost_time": sum(h.get("cost_time", 0) for h in summary["history"]),
                "step_results": [
                    {"skill": h["skill"], "robot": "UAV_1", "success": h["success"],
                     "cost_time": h.get("cost_time", 0), "error": h.get("error")}
                    for h in summary["history"]
                ],
                "agent_iterations": summary["iterations"],
            })

            # 生成 HTML 巡检报告
            try:
                _generate_patrol_report(task, summary, final_result, ok)
            except Exception as rpt_e:
                logger.warning(f"报告生成失败: {rpt_e}")

        except Exception as e:
            logger.exception("AI 任务执行异常")
            state.push_log("error", f"AI 任务异常: {e}")
            socketio.emit("ai_plan_result", {"ok": False, "error": str(e)})
        finally:
            state.is_executing = False
            socketio.emit("system_status", _get_system_status())

    t = threading.Thread(target=_run_ai, daemon=True)
    t.start()


# ── AI 对话聊天 ──────────────────────────────────────────────────────────────

# 对话历史 (server 端维护, 每个 session 独立)
_chat_histories: dict = {}  # {sid: [{"role": str, "content": str}]}


@socketio.on("ai_chat")
def on_ai_chat(data):
    """
    统一对话入口: LLM 自己决定是聊天还是执行任务。
    不做硬编码意图识别, 让模型自主判断。
    data: {"message": "..."}
    """
    if not state.initialized:
        emit("ai_chat_reply", {"ok": False, "error": "系统未初始化"})
        return

    message = data.get("message", "").strip()
    if not message:
        emit("ai_chat_reply", {"ok": False, "error": "消息不能为空"})
        return

    sid = request.sid
    from skills.cognitive_skills import AskUser
    if AskUser._answer_event and not AskUser._answer_event.is_set():
        AskUser.receive_answer(message)
        state.push_log(
            "info",
            f"Operator answered pending question: {message[:80]}",
            {"intent": "ask_user_answer"},
        )
        emit("ai_chat_reply", {
            "ok": True,
            "intent": "ANSWER",
            "reply": f"Answer received: {message[:80]}",
            "message": message,
        })
        return

    def _reply():
        from brain.chat_mode import unified_chat
        from llm_client import get_client

        if sid not in _chat_histories:
            _chat_histories[sid] = []
        history = _chat_histories[sid]

        client = get_client(module="planner")

        # 收集上下文: 感知摘要 + 世界状态 + 技能表
        perception_summary = ""
        try:
            from perception.daemon import get_daemon
            daemon = get_daemon()
            if daemon and daemon.is_running:
                perception_summary = daemon.get_summary()
        except ImportError:
            pass

        world_state = state.world_model.get_world_state()
        world_lines = []
        for rid, rd in world_state.get("robots", {}).items():
            pos = rd.get("position", [0, 0, 0])
            world_lines.append(
                f"{rid}: 位置={pos}, 电量={rd.get('battery', '?')}%, "
                f"状态={rd.get('status', '?')}, 在空中={rd.get('in_air', '?')}"
            )
        world_state_str = "\n".join(world_lines) if world_lines else "(无)"

        # 技能表
        skill_table = ""
        reg = state.robot_registries.get(state.current_robot)
        if reg:
            try:
                from skills.skill_loader import build_skill_summary
                skill_table = build_skill_summary(reg.get_skill_catalog())
            except Exception:
                pass

        # 尝试获取最近的相机视觉描述 (VLM)
        camera_description = ""
        try:
            from perception.daemon import get_daemon
            daemon = get_daemon()
            if daemon and daemon.is_running:
                detailed = daemon.get_detailed_summary()
                camera_description = detailed.get("vlm", "")
        except Exception:
            pass

        # 统一调用 LLM
        result = unified_chat(
            user_input=message,
            chat_history=history,
            llm_client=client,
            skill_table=skill_table,
            perception_summary=perception_summary,
            world_state_str=world_state_str,
            camera_description=camera_description,
        )

        # 更新历史
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result["text"]})
        if len(history) > 40:
            _chat_histories[sid] = history[-40:]

        if result["type"] == "plan" and result["plan"] and state.mode == "ai":
            # LLM 决定执行任务。若是本地单动作兜底计划，直接执行该
            # plan，避免再进入依赖 LLM 的 AgentLoop 后因模型通道失败卡住。
            socketio.emit("ai_chat_reply", {
                "ok": True,
                "intent": "TASK",
                "reply": result["text"],
                "message": message,
            }, to=sid)

            if result.get("fallback") == "single_action":
                state.push_log("info", f"🤖 执行本地单动作兜底计划: {message}")
                _execute_plan_from_chat(message, result["plan"], sid)
            else:
                state.push_log("info", f"🤖 启动自主任务: {message}")
                # 启动 AgentLoop
                _run_agent_loop(message, sid)
        else:
            # 纯对话
            socketio.emit("ai_chat_reply", {
                "ok": True,
                "intent": "CHAT",
                "reply": result["text"],
                "message": message,
            }, to=sid)

    t = threading.Thread(target=_reply, daemon=True)
    t.start()




def _generate_patrol_report(task, summary, final_result, success):
    """生成 HTML 巡检报告，保存到 static/reports/ 并通知前端。"""
    import datetime, os, html as html_mod
    from skills.cognitive_skills import Report

    reports = Report._reports.copy()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(_BASE_DIR, "ui", "dist", "reports")
    os.makedirs(report_dir, exist_ok=True)
    filename = f"patrol_report_{ts}.html"
    filepath = os.path.join(report_dir, filename)

    total_time = sum(h.get("cost_time", 0) for h in summary["history"])
    steps = summary["history"]
    iterations = summary["iterations"]

    # 构建 HTML
    h = []
    h.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    h.append("<title>AeroWeaver 巡检报告</title>")
    h.append("<style>")
    h.append("body{font-family:'Segoe UI',sans-serif;max-width:900px;margin:40px auto;padding:20px;background:#0d1117;color:#c9d1d9}")
    h.append("h1{color:#58a6ff;border-bottom:2px solid #21262d;padding-bottom:12px}")
    h.append("h2{color:#79c0ff;margin-top:30px}")
    h.append(".meta{background:#161b22;padding:15px;border-radius:8px;margin:15px 0;border:1px solid #21262d}")
    h.append(".meta span{display:inline-block;margin-right:25px;color:#8b949e}")
    h.append(".meta b{color:#c9d1d9}")
    h.append(".report-card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:15px;margin:10px 0}")
    h.append(".report-card.warning{border-left:4px solid #d29922}")
    h.append(".report-card.danger{border-left:4px solid #f85149}")
    h.append(".report-card.info{border-left:4px solid #58a6ff}")
    h.append(".badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600}")
    h.append(".badge.ok{background:#238636;color:#fff} .badge.fail{background:#da3633;color:#fff}")
    h.append(".badge.info{background:#1f6feb;color:#fff} .badge.warning{background:#9e6a03;color:#fff}")
    h.append("table{width:100%;border-collapse:collapse;margin:15px 0}")
    h.append("th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #21262d}")
    h.append("th{color:#8b949e;font-weight:600}")
    h.append(".footer{text-align:center;color:#484f58;margin-top:40px;font-size:12px}")
    h.append("</style></head><body>")

    status = "✅ 任务完成" if success else "任务未完成"
    h.append(f"<h1>🚁 AeroWeaver 巡检报告</h1>")
    h.append(f"<div class='meta'>")
    h.append(f"<span>状态: <b>{status}</b></span>")
    h.append(f"<span>时间: <b>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</b></span>")
    h.append(f"<span>耗时: <b>{total_time:.1f}s</b></span>")
    h.append(f"<span>执行步骤: <b>{summary['successful']}/{summary['total_actions']}</b></span>")
    h.append(f"<span>AI 决策轮数: <b>{iterations}</b></span>")
    h.append(f"</div>")

    h.append(f"<h2>📋 任务目标</h2>")
    h.append(f"<div class='report-card info'>{html_mod.escape(task)}</div>")

    # AI 总结
    if final_result.get("summary"):
        h.append(f"<h2>🧠 AI 总结</h2>")
        h.append(f"<div class='report-card info'>{html_mod.escape(final_result['summary'])}</div>")

    # 巡检发现
    if reports:
        h.append(f"<h2>🔍 巡检发现 ({len(reports)} 条)</h2>")
        for r in reports:
            sev = r.get("severity", "info")
            icon = {"info": "📋", "warning": "⚠️", "danger": "🚨"}.get(sev, "📋")
            h.append(f"<div class='report-card {sev}'>")
            h.append(f"<span class='badge {sev}'>{sev.upper()}</span> ")
            h.append(f"<b>{icon} #{r.get('id','')} [{r.get('time','')}] {r.get('position','')}</b><br>")
            h.append(f"{html_mod.escape(r.get('content', ''))}")
            h.append(f"</div>")

    # 执行明细
    h.append(f"<h2>⚙️ 执行明细</h2>")
    h.append(f"<table><tr><th>#</th><th>技能</th><th>结果</th><th>耗时</th></tr>")
    for i, s in enumerate(steps, 1):
        badge = "<span class='badge ok'>OK</span>" if s["success"] else f"<span class='badge fail'>FAIL</span>"
        h.append(f"<tr><td>{i}</td><td>{s['skill']}</td><td>{badge}</td><td>{s.get('cost_time',0):.1f}s</td></tr>")
    h.append("</table>")

    h.append(f"<div class='footer'>Generated by AeroWeaver v2.0 — Autonomous UAV Framework</div>")
    h.append("</body></html>")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(h))

    report_url = f"/reports/{filename}"
    logger.info(f"📊 巡检报告已生成: {report_url}")

    socketio.emit("ai_chat_reply", {
        "ok": True,
        "reply": f"📊 **巡检报告已生成！** [点击查看]({report_url})",
        "intent": "final_report",
        "report_url": report_url,
    })


def _run_agent_loop(goal, sid):
    """
    启动自主 Agent 循环。观察→思考→行动→反思, 直到目标达成。
    """
    if state.is_executing:
        # 先检查是否是对 ask_user 的回答
        from skills.cognitive_skills import AskUser
        if AskUser._answer_event and not AskUser._answer_event.is_set():
            AskUser.receive_answer(goal)
            socketio.emit("ai_chat_reply", {
                "ok": True, "intent": "ANSWER",
                "reply": f"✅ 已收到你的回答: \"{goal[:80]}\"",
                "message": goal,
            }, to=sid)
            return

        # 任务执行中, 注入用户消息到 AgentLoop
        if state._current_agent_loop:
            state._current_agent_loop.inject_user_message(goal)
            socketio.emit("ai_chat_reply", {
                "ok": True, "intent": "INJECT",
                "reply": f"收到, 已将你的指令注入到当前任务: \"{goal[:50]}\"",
                "message": goal,
            }, to=sid)
        else:
            socketio.emit("ai_chat_reply", {
                "ok": True, "intent": "CHAT",
                "reply": "我正在执行另一个任务, 请等我完成。",
                "message": goal,
            }, to=sid)
        return

    state.is_executing = True
    state._ai_stop_event.clear()
    socketio.emit("system_status", _get_system_status())

    try:
        from brain.agent_loop import AgentLoop
        from llm_client import get_client

        client = get_client(module="planner")
        reg = state.robot_registries.get(state.current_robot)

        def on_thinking(iteration, output):
            thinking = output.get("thinking", "")
            decision = output.get("decision", "")
            progress = output.get("goal_progress", "")
            action = output.get("action", {})
            reflection = output.get("reflection")
            # 新事件：结构化思考链
            socketio.emit("ai_thought", {
                "iteration": iteration,
                "thinking": thinking,
                "decision": decision,
                "reflection": reflection,
                "progress": progress,
                "skill": action.get("skill", ""),
                "parameters": action.get("parameters", {}),
            })
            socketio.emit("ai_thinking", {
                "phase": "thinking",
                "detail": f"[第{iteration}轮] {thinking}",
                "decision": decision,
            })
            # 把思考过程推送到聊天
            socketio.emit("ai_chat_reply", {
                "ok": True, "intent": "THINKING",
                "reply": f"💭 {thinking}" + (f"\n📊 {progress}" if progress else ""),
                "message": goal,
            }, to=sid)
            state.push_log("info", f"🧠 第{iteration}轮: {thinking}")

        def on_action(iteration, skill, params, result):
            status = "✅" if result.success else "❌"
            msg = f"{status} {skill}"
            if not result.success:
                msg += f" - {result.error_msg}"
            state.push_log("success" if result.success else "error", f"  {msg} ({result.cost_time:.1f}s)")
            socketio.emit("world_state", state.get_world_snapshot())

        def on_complete(success, summary):
            socketio.emit("ai_thinking", {"phase": "idle", "detail": ""})
            status = "✅ 目标达成" if success else "⚠️ 任务结束"
            state.push_log("success" if success else "warn", f"{status}: {summary[:80]}")
            socketio.emit("ai_chat_reply", {
                "ok": True, "intent": "RESULT",
                "reply": f"{status}\n{summary}",
                "message": goal,
            }, to=sid)

        loop = AgentLoop(
            goal=goal,
            llm_client=client,
            runtime=state.runtime,
            world_model=state.world_model,
            skill_registry=reg,
            max_iterations=50,
            on_thinking=on_thinking,
            on_action=on_action,
            on_complete=on_complete,
            on_stream=lambda token: socketio.emit("ai_stream", {"token": token, "done": False}),
            stop_event=state._ai_stop_event,
            experience_store=getattr(state, "experience_store", None),
        )
        # 注入被动感知引擎引用
        try:
            from skills.perception_skills import _get_passive_perception
            loop._passive_engine = _get_passive_perception()
        except Exception:
            loop._passive_engine = None
        loop.run()
        socketio.emit("ai_stream", {"token": "", "done": True})

        # 推送执行报告
        summary = loop.get_summary()
        socketio.emit("ai_execution_report", {
            "ok": summary["failed"] == 0 and summary["total_actions"] > 0,
            "task": goal,
            "completed_steps": summary["successful"],
            "total_steps": summary["total_actions"],
            "replans": 0,
            "cost_time": sum(h.get("cost_time", 0) for h in summary["history"]),
            "step_results": [
                {"skill": h["skill"], "robot": "UAV_1", "success": h["success"],
                 "cost_time": h.get("cost_time", 0), "error": h.get("error")}
                for h in summary["history"]
            ],
            "agent_iterations": summary["iterations"],
        })
        socketio.emit("world_state", state.get_world_snapshot())
        socketio.emit("skill_catalog", _get_skill_catalog())

    except Exception as e:
        logger.exception("AgentLoop 异常")
        state.push_log("error", f"Agent 异常: {e}")
    finally:
        state.is_executing = False
        socketio.emit("system_status", _get_system_status())


def _execute_plan_from_chat(task, steps, sid):
    """
    从对话中触发的任务执行。
    复用 ai_task 的逐步执行 + 重规划逻辑。
    """
    if state.is_executing:
        socketio.emit("ai_chat_reply", {
            "ok": True, "intent": "CHAT",
            "reply": "我正在执行另一个任务, 请等我完成。",
            "message": task,
        }, to=sid)
        return

    state.is_executing = True
    state._ai_stop_event.clear()
    socketio.emit("system_status", _get_system_status())

    try:
        import time as _time
        MAX_REPLANS = 3
        replan_count = 0
        all_step_results = []
        final_success = False

        while replan_count <= MAX_REPLANS:
            state.push_log("info", f"🚀 执行 {len(steps)} 步" + (f" (重规划第{replan_count}次)" if replan_count > 0 else ""))
            total = len(steps)
            completed = 0
            failed_step = None
            failed_error = None

            for i, step_data in enumerate(steps):
                if state._ai_stop_event.is_set():
                    break

                skill_name = step_data.get("skill", "?")
                step_num = step_data.get("step", i + 1)
                socketio.emit("ai_thinking", {
                    "phase": "executing",
                    "detail": f"执行步骤 {step_num}/{total}: {skill_name}",
                    "current_step": step_num,
                    "total_steps": total,
                    "skill": skill_name,
                })

                result = state.runtime.dispatch_skill(step_data)
                all_step_results.append((step_data, result))

                robot_id = step_data.get("robot", state.current_robot)
                robot_reg = state.robot_registries.get(robot_id)
                if robot_reg:
                    robot_reg.update_execution_status(skill_name, result.success)

                if result.success:
                    completed += 1
                    state.push_log("success", f"✅ 步骤 {step_num}: {skill_name} ({result.cost_time:.1f}s)")
                else:
                    failed_step = step_data
                    failed_error = result.error_msg
                    state.push_log("error", f"❌ 步骤 {step_num}: {skill_name} - {result.error_msg}")
                    break

            if completed == total:
                final_success = True
                break
            if state._ai_stop_event.is_set():
                break

            # 重规划
            if failed_step and replan_count < MAX_REPLANS:
                replan_count += 1
                state.push_log("info", f"🔄 重规划 (第{replan_count}次)...")
                socketio.emit("ai_thinking", {"phase": "replanning", "detail": f"{failed_step.get('skill','?')} 失败, 重新规划..."})

                from brain.chat_mode import unified_chat
                from llm_client import get_client
                client = get_client(module="planner")

                world_state = state.world_model.get_world_state()
                w_lines = [f"{rid}: pos={rd.get('position')}, battery={rd.get('battery')}%, status={rd.get('status')}" for rid, rd in world_state.get("robots", {}).items()]

                history_str = "\n".join(f"  - {sd.get('skill','?')}: {'成功' if r.success else f'失败({r.error_msg})'}" for sd, r in all_step_results)
                replan_msg = f"刚才执行任务\"{task}\"时, 出了问题:\n{history_str}\n\n请根据当前状态重新规划。已成功的步骤不需要重复。"

                skill_table = ""
                reg = state.robot_registries.get(state.current_robot)
                if reg:
                    try:
                        from skills.skill_loader import build_skill_summary
                        skill_table = build_skill_summary(reg.get_skill_catalog())
                    except Exception:
                        pass

                replan_result = unified_chat(
                    user_input=replan_msg,
                    chat_history=[],
                    llm_client=client,
                    skill_table=skill_table,
                    world_state_str="\n".join(w_lines),
                )

                if replan_result["type"] == "plan" and replan_result["plan"]:
                    steps = replan_result["plan"]
                    state.push_log("info", f"📋 重规划: {len(steps)} 步 | {replan_result['text'][:60]}")
                    socketio.emit("ai_plan_result", {"ok": True, "task": task, "reasoning": f"[重规划] {replan_result['text']}", "steps": steps})
                else:
                    state.push_log("warn", f"重规划未产生计划: {replan_result['text'][:60]}")
                    break
            else:
                if replan_count >= MAX_REPLANS:
                    state.push_log("error", f"已达最大重规划次数 ({MAX_REPLANS})")
                break

        socketio.emit("ai_thinking", {"phase": "idle", "detail": ""})

        total_completed = sum(1 for _, r in all_step_results if r.success)
        status = "✅ 成功" if final_success else "❌ 失败"
        replan_note = f" (重规划{replan_count}次)" if replan_count > 0 else ""
        state.push_log("success" if final_success else "error", f"{status} | 完成 {total_completed}/{len(all_step_results)} 步{replan_note}")

        socketio.emit("ai_execution_report", {
            "ok": final_success, "task": task,
            "completed_steps": total_completed, "total_steps": len(all_step_results),
            "replans": replan_count,
            "cost_time": sum(r.cost_time for _, r in all_step_results),
            "step_results": [{"skill": sd.get("skill", "?"), "robot": sd.get("robot", state.current_robot), "success": r.success, "cost_time": r.cost_time, "error": r.error_msg if hasattr(r, "error_msg") else None} for sd, r in all_step_results],
        })

        # 把执行结果也推送到对话
        result_msg = f"{'任务完成' if final_success else '任务未完成'}: {total_completed}/{len(all_step_results)} 步成功{replan_note}"
        socketio.emit("ai_chat_reply", {"ok": True, "intent": "RESULT", "reply": result_msg, "message": task}, to=sid)
        socketio.emit("world_state", state.get_world_snapshot())
        socketio.emit("skill_catalog", _get_skill_catalog())

    except Exception as e:
        logger.exception("对话任务执行异常")
        state.push_log("error", f"执行异常: {e}")
    finally:
        state.is_executing = False
        socketio.emit("system_status", _get_system_status())


@socketio.on("stop_execution")
def on_stop_execution():
    """中止当前执行：通知 adapter 停止飞行 + 重置执行状态。"""
    state._ai_stop_event.set()

    # 通知 adapter 立即停止飞行
    try:
        from adapters.adapter_manager import get_all_adapters
        for adapter in get_all_adapters():
            if adapter and hasattr(adapter, 'request_stop'):
                adapter.request_stop()
        logger.info("🛑 已向全部活动无人机发送飞行打断信号")
    except Exception:
        pass

    # 强制重置执行状态
    was_executing = state.is_executing or bool(state.executing_robot_snapshot())
    state.is_executing = False
    socketio.emit("system_status", _get_system_status())

    if was_executing:
        state.push_log("warn", "⏹ 执行已打断，尝试悬停...")
        # 后台让无人机悬停
        def _hold():
            try:
                from adapters.adapter_manager import get_adapter
                adapter = get_adapter()
                if adapter and _adapter_connected(adapter) and adapter.is_in_air():
                    result = adapter.hover(2.0)
                    state.push_log("info", f"🔄 悬停中: {result.message}")
                else:
                    state.push_log("info", "无人机不在空中，无需悬停")
            except Exception as e:
                state.push_log("warn", f"悬停失败: {e}")
        threading.Thread(target=_hold, daemon=True).start()
    else:
        state.push_log("info", "⏹ 当前无执行中的任务")

    emit("system_status", _get_system_status())


@socketio.on("velocity_control")
def on_velocity_control(data):
    """
    驾驶舱实时速度控制 (Body 坐标系)。
    data: {"forward": 0, "right": 0, "down": 0, "yaw_rate": 0}
    所有值为 m/s 或 deg/s，0 = 停止。
    """
    if not state.initialized:
        emit("velocity_result", {"ok": False, "error": "系统未初始化"})
        return

    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    if not adapter or not _adapter_connected(adapter):
        emit("velocity_result", {"ok": False, "error": "适配器未连接"})
        return

    fwd   = float(data.get("forward", 0))
    right = float(data.get("right", 0))
    down  = float(data.get("down", 0))
    yaw   = float(data.get("yaw_rate", 0))

    robot_id = data.get("robot_id") or state.current_robot
    set_velocity_for = getattr(adapter, "set_velocity_body_for", None)
    stop_velocity_for = getattr(adapter, "stop_velocity_for", None)

    # 全 0 = 停止
    if callable(set_velocity_for):
        if fwd == 0 and right == 0 and down == 0 and yaw == 0:
            result = stop_velocity_for(robot_id)
        else:
            result = set_velocity_for(robot_id, fwd, right, down, yaw_rate=yaw)
    else:
        set_active_robot = getattr(adapter, "set_active_robot", None)
        if callable(set_active_robot):
            set_active_robot(robot_id)
        if fwd == 0 and right == 0 and down == 0 and yaw == 0:
            result = adapter.stop_velocity()
        else:
            result = adapter.set_velocity_body(fwd, right, down, yaw_rate=yaw)
    emit("velocity_result", {
        "ok": result.success,
        "robot_id": robot_id,
        "msg": result.message,
    })


@socketio.on("get_telemetry")
def on_get_telemetry(data=None):
    """返回实时遥测数据（位置/速度/电池/姿态）。"""
    if not state.initialized:
        emit("telemetry", {})
        return
    from adapters.adapter_manager import get_adapter
    adapter = get_adapter()
    if not adapter or not _adapter_connected(adapter):
        emit("telemetry", {})
        return
    try:
        data = data or {}
        robot_id = data.get("robot_id") or state.current_robot
        vehicle_for_robot = getattr(adapter, "vehicle_for_robot", None)
        client = getattr(adapter, "_client", None)
        if callable(vehicle_for_robot) and client and hasattr(client, "sim_get_object_pose"):
            vehicle = vehicle_for_robot(robot_id)
            pose = client.sim_get_object_pose(vehicle) or {}
            raw_position = pose.get("position", {})
            north = float(raw_position.get("x_val", 0.0))
            east = float(raw_position.get("y_val", 0.0))
            down = float(raw_position.get("z_val", 0.0))
            emit("telemetry", {
                "robot_id": robot_id,
                "vehicle": vehicle,
                "position": {
                    "north": round(north, 2),
                    "east": round(east, 2),
                    "down": round(down, 2),
                },
                "altitude": round(-down, 2),
                "battery": 100.0,
                "in_air": down < -1.0,
                "armed": True,
            })
            return

        pos = adapter.get_position()
        bat = adapter.get_battery()
        in_air = adapter.is_in_air()
        armed = adapter.is_armed()
        emit("telemetry", {
            "position": {"north": round(pos.north, 2), "east": round(pos.east, 2), "down": round(pos.down, 2)},
            "altitude": round(-pos.down, 2),
            "battery": round(min(bat[1], 100) if bat[1] > 1 else bat[1] * 100, 1) if bat else None,
            "in_air": in_air,
            "armed": armed,
        })
    except Exception as e:
        logger.warning("遥测获取失败: %s", e)
        emit("telemetry", {})


@socketio.on("get_world_state")
def on_get_world_state():
    emit("world_state", state.get_world_snapshot())


@socketio.on("get_skill_catalog")
def on_get_skill_catalog():
    emit("skill_catalog", _get_skill_catalog())


@socketio.on("update_robot_position")
def on_update_robot_position(data):
    """
    更新机器人位置（供仿真器推送位置更新）。
    data: {"robot_id": "UAV_1", "position": [x, y, z], "battery": 90.0}
    """
    if not state.world_model:
        return
    robot_id = data.get("robot_id")
    if not robot_id:
        return
    update = {"robots": {robot_id: {}}}
    if "position" in data:
        update["robots"][robot_id]["position"] = data["position"]
    if "battery" in data:
        update["robots"][robot_id]["battery"] = data["battery"]
    if "status" in data:
        update["robots"][robot_id]["status"] = data["status"]
    state.world_model.update_world_state(update)
    socketio.emit("world_state", state.get_world_snapshot())


@socketio.on("register_robot")
def on_register_robot(data):
    """
    动态注册新机器人（无需重启服务）。
    data: {
        "robot_id": "UAV_2",
        "robot_type": "UAV",          # "UAV" | "UGV" | ...
        "initial_position": [0, 0, 0],
        "battery": 100.0
    }
    新机器人注册后广播 robot_joined 事件，前端自动渲染新卡片。
    """
    if not state.initialized or not state.world_model:
        emit("register_robot_result", {"ok": False, "error": "系统未初始化"})
        return

    robot_id   = data.get("robot_id", "").strip()
    robot_type = data.get("robot_type", "UAV").upper()
    position   = data.get("initial_position", [0, 0, 0])
    battery    = float(data.get("battery", 100.0))

    if not robot_id:
        emit("register_robot_result", {"ok": False, "error": "robot_id 不能为空"})
        return

    if os.getenv("SIM_ADAPTER", "px4").lower() in ("airsim", "airsim_physics") and robot_type == "UAV":
        try:
            from adapters.adapter_manager import get_adapter
            adapter = get_adapter()
            client = getattr(adapter, "_client", None) if adapter else None
            vehicles = list(client.list_vehicles() or []) if client else []
            primary = getattr(adapter, "_vehicle_name", "")
            if primary and primary not in vehicles:
                vehicles.insert(0, primary)
            live_robot_ids = {
                f"UAV_{idx + 1}"
                for idx, _vehicle in enumerate(sorted([str(v) for v in vehicles if str(v).strip()], key=_vehicle_sort_key))
            }
            if robot_id not in live_robot_ids:
                if adapter and client:
                    _sync_airsim_fleet_to_world(adapter)
                    socketio.emit("world_state", state.get_world_snapshot())
                emit("register_robot_result", {
                    "ok": False,
                    "robot_id": robot_id,
                    "error": f"{robot_id} not present in AirSim vehicles",
                    "live_robot_ids": sorted(live_robot_ids, key=_vehicle_sort_key),
                })
                return
        except Exception as exc:
            logger.debug("AirSim register_robot guard failed: %s", exc)

    # 已存在则只更新状态，不重复广播 robot_joined
    world = state.world_model.get_world_state()
    already_exists = robot_id in world.get("robots", {})

    state.world_model.register_robot(robot_id, robot_type,
                                     initial_position=position,
                                     battery=battery)

    # 为新机器人构建独立技能注册表（已存在则重建，保持最新）
    reg, count = _build_robot_registry(robot_id, robot_type)
    state.robot_registries[robot_id] = reg
    # 注入到 runtime 的 executor
    if state.runtime:
        state.runtime._robot_registries[robot_id] = reg
        state.runtime._executor._robot_registries[robot_id] = reg

    state.push_log("info", f"{'更新' if already_exists else '新增'}机器人: {robot_id} ({robot_type})")
    emit("register_robot_result", {"ok": True, "robot_id": robot_id, "already_existed": already_exists})

    # 只有真正新加入的机器人才广播 robot_joined
    if not already_exists:
        robot_info = {
            "robot_id":   robot_id,
            "robot_type": robot_type,
            "position":   position,
            "battery":    battery,
        }
        socketio.emit("robot_joined", robot_info)
        state.push_log("success", f"✅ 机器人 {robot_id} ({robot_type}) 已加入编队")

    # 广播最新世界状态
    socketio.emit("world_state", state.get_world_snapshot())


# ══════════════════════════════════════════════════════════════════════════════
#  世界状态定时推送（每 2 秒广播一次）
# ══════════════════════════════════════════════════════════════════════════════



def _world_state_broadcaster():
    while True:
        time.sleep(2)
        if state.initialized:
            socketio.emit("world_state", state.get_world_snapshot())



# ── Memory API ────────────────────────────────────────────────────────────────

_memory_manager_singleton = None

def _get_memory_manager():
    """记忆管理器单例，避免重复初始化"""
    global _memory_manager_singleton
    if _memory_manager_singleton is None:
        from memory.memory_manager import MemoryManager
        _memory_manager_singleton = MemoryManager()
    return _memory_manager_singleton


@app.route("/api/memory/stats", methods=["GET"])
def api_memory_stats():
    """记忆系统统计"""
    try:
        mm = _get_memory_manager()
        return jsonify({
            "ok": True,
            "layers": {
                "working": {"count": len(mm.working.get_recent(100)), "label": "Working"},
                "episodic": {"count": mm.episodic.count(), "label": "Episodic"},
                "skill": {"count": mm.skill.count(), "label": "Skill"},
                "world": {"count": mm.world.count(), "label": "World"},
            },
        })
    except Exception as e:
        return jsonify({"ok": True, "layers": {
            "working": {"count": 0, "label": "Working"},
            "episodic": {"count": 0, "label": "Episodic"},
            "skill": {"count": 0, "label": "Skill"},
            "world": {"count": 0, "label": "World"},
        }})


@app.route("/api/memory/recent", methods=["GET"])
def api_memory_recent():
    """最近记忆"""
    try:
        mm = _get_memory_manager()
        items = mm.working.get_recent(20)
        return jsonify({"ok": True, "items": [
            {"text": str(item), "score": 0, "layer": "working", "metadata": {}}
            for item in items
        ]})
    except Exception as e:
        return jsonify({"ok": True, "items": []})


@app.route("/api/memory/search", methods=["POST"])
def api_memory_search():
    """记忆语义搜索"""
    data = request.get_json() or {}
    query = data.get("query", "")
    top_k = data.get("top_k", 10)
    if not query:
        return jsonify({"ok": False, "error": "query 不能为空"}), 400
    try:
        mm = _get_memory_manager()
        items = mm.recall(query, top_k=top_k)
        return jsonify({"ok": True, "query": query, "items": [
            {"text": i.text, "score": i.score, "layer": i.metadata.get("layer", "unknown"),
             "metadata": i.metadata} for i in items
        ]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/map/landmarks")
def api_map_landmarks():
    """返回 WORLD_MAP.md 中的地标列表 (NED 坐标)"""
    import re as _re
    map_path = os.path.join(_BASE_DIR, "robot_profile", "WORLD_MAP.md")
    landmarks = []
    if os.path.exists(map_path):
        text = open(map_path, "r", encoding="utf-8").read()
        # 解析表格行: | name | (n, e) | desc |
        for m in _re.finditer(r"\|\s*(.+?)\s*\|\s*\((-?[\d.]+),\s*(-?[\d.]+)\)\s*\|\s*(.+?)\s*\|", text):
            name = m.group(1).strip().strip("|").strip()
            if name in ("物体", "地标", "---", ""):
                continue
            landmarks.append({
                "name": name,
                "n": float(m.group(2)),
                "e": float(m.group(3)),
                "desc": m.group(4).strip(),
            })
    return jsonify({"landmarks": landmarks})


# ── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 先加载配置（确保 .env 被解析）
    import config as _cfg

    # v2.0: 统一日志
    from core.logger import setup_logging
    setup_logging(log_dir="logs", level="INFO")

    # 启动世界状态定时推送
    t = threading.Thread(target=_world_state_broadcaster, daemon=True)
    t.start()

    # 自动初始化
    init_thread = threading.Thread(target=_do_init, daemon=True)
    init_thread.start()

    server_port = int(os.getenv("AEROWEAVER_PORT", "5001"))
    logger.info(f"AeroWeaver 控制台服务启动于 http://localhost:{server_port}")

    socketio.run(app, host="0.0.0.0", port=server_port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
