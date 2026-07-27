"""
adapter_manager.py
适配器管理器 —— 全局单例，管理当前活跃的仿真适配器。

用法:
    from adapters.adapter_manager import get_adapter, init_adapter

    init_adapter("px4", connection_str="udp://:14540")  # 启动时调用一次
    adapter = get_adapter()                              # 硬技能里获取适配器
    result = adapter.takeoff(5.0)                       # 调用统一接口
"""

import logging
import os
import threading
from contextlib import contextmanager
from typing import Optional
from adapters.sim_adapter import SimAdapter

logger = logging.getLogger(__name__)

# ── 全局单例 ──────────────────────────────────────────────────────────────────

_adapter: Optional[SimAdapter] = None
_adapter_type: str = ""
_adapter_connection_str: str = ""
_adapter_timeout: float = 15.0
_adapter_context = threading.local()
_robot_adapters: dict[str, SimAdapter] = {}
_robot_adapters_lock = threading.RLock()

# ── 适配器注册表 ──────────────────────────────────────────────────────────────

_ADAPTER_REGISTRY: dict = {}


def register_adapter(name: str, adapter_class):
    """注册一个适配器类型。"""
    _ADAPTER_REGISTRY[name] = adapter_class


def list_adapters() -> list:
    """列出所有已注册的适配器类型。"""
    return [
        {"name": name, "description": cls.description, "vehicles": cls.supported_vehicles}
        for name, cls in _ADAPTER_REGISTRY.items()
    ]


# ── 内置适配器注册 ────────────────────────────────────────────────────────────

def _register_builtins():
    from adapters.mock_adapter import MockAdapter
    register_adapter("mock", MockAdapter)
    try:
        from adapters.px4_adapter import PX4Adapter
        register_adapter("px4", PX4Adapter)
    except ImportError:
        pass  # mavsdk 未安装时跳过
    try:
        from adapters.airsim_adapter import AirSimAdapter
        register_adapter("airsim", AirSimAdapter)
    except ImportError:
        pass  # airsim 包未安装时跳过
    try:
        from adapters.airsim_physics import AirSimPhysicsAdapter
        register_adapter("airsim_physics", AirSimPhysicsAdapter)
    except ImportError:
        pass
    try:
        from adapters.mavsdk_adapter import MavsdkAdapter
        register_adapter("mavsdk", MavsdkAdapter)
    except ImportError:
        pass
    try:
        from adapters.gazebo_direct_adapter import GazeboDirectAdapter
        register_adapter("gazebo_direct", GazeboDirectAdapter)
    except ImportError:
        pass

_register_builtins()


# ── 公共接口 ──────────────────────────────────────────────────────────────────

def init_adapter(adapter_type: str = "px4", connection_str: str = "", timeout: float = 15.0) -> bool:
    """
    初始化并连接仿真适配器。

    Args:
        adapter_type: 适配器类型名（"px4" / "mock" / 自定义）
        connection_str: 连接字符串（每种适配器有默认值）
        timeout: 连接超时

    Returns:
        bool: 是否连接成功
    """
    global _adapter, _adapter_type, _adapter_connection_str, _adapter_timeout

    cls = _ADAPTER_REGISTRY.get(adapter_type)
    if cls is None:
        logger.error(f"未知适配器类型: {adapter_type}，可选: {list(_ADAPTER_REGISTRY.keys())}")
        return False

    _close_robot_adapters()
    _adapter_type = adapter_type
    _adapter_connection_str = connection_str or ""
    _adapter_timeout = timeout
    _adapter = cls()
    logger.info(f"初始化适配器: {_adapter.name} ({_adapter.description})")

    ok = _adapter.connect(connection_str or "", timeout)
    if ok:
        logger.info(f"✅ 适配器 {_adapter.name} 连接成功")
    else:
        allow_fallback = os.getenv("AEROWEAVER_ALLOW_MOCK_FALLBACK", "0") == "1"
        if adapter_type != "mock" and not allow_fallback:
            logger.error(
                "❌ 适配器 %s 连接失败；保持该适配器为 disconnected，禁止静默降级到 mock。"
                "如确实需要 mock，请显式设置 SIM_ADAPTER=mock 或 AEROWEAVER_ALLOW_MOCK_FALLBACK=1。",
                _adapter.name,
            )
        else:
            logger.warning(f"⚠️ 适配器 {_adapter.name} 连接失败，降级到 mock")
            from adapters.mock_adapter import MockAdapter
            _adapter = MockAdapter()
            _adapter.connect()

    return ok


def get_adapter() -> Optional[SimAdapter]:
    """获取当前活跃的适配器实例。"""
    robot_id = getattr(_adapter_context, "robot_id", None)
    if robot_id:
        return _get_robot_adapter(robot_id)
    return _adapter


def get_primary_adapter() -> Optional[SimAdapter]:
    """Return the shared telemetry/control adapter without thread routing."""
    return _adapter


def get_robot_adapter(robot_id: str) -> Optional[SimAdapter]:
    """Return the adapter dedicated to one robot execution channel."""
    return _get_robot_adapter(robot_id)


def _adapter_is_connected(adapter) -> bool:
    if adapter is None:
        return False
    connected = getattr(adapter, "is_connected", False)
    try:
        return bool(connected() if callable(connected) else connected)
    except Exception:
        return False


def _get_robot_adapter(robot_id: str) -> Optional[SimAdapter]:
    """Return an isolated AirSim adapter for one execution thread."""
    if (
        _adapter is None
        or getattr(_adapter, "name", "") != "airsim_openfly"
        or not _adapter_is_connected(_adapter)
    ):
        return _adapter

    key = str(robot_id or "UAV_1")
    with _robot_adapters_lock:
        cached = _robot_adapters.get(key)
        if cached is not None and _adapter_is_connected(cached):
            return cached

    # Connecting to AirSim can take over a second. Do it outside the cache lock
    # so two different UAVs can initialize their RPC channels in parallel.
    vehicle_for_robot = getattr(_adapter, "vehicle_for_robot", None)
    vehicle_name = (
        vehicle_for_robot(key)
        if callable(vehicle_for_robot)
        else getattr(_adapter, "_vehicle_name", None)
    )
    isolated = type(_adapter)(vehicle_name=vehicle_name)
    connection_str = _adapter_connection_str
    if not connection_str:
        host = getattr(_adapter, "_airsim_host", "127.0.0.1")
        port = getattr(_adapter, "_airsim_port", 41451)
        connection_str = f"{host}:{port}"
    if not isolated.connect(connection_str, _adapter_timeout):
        raise ConnectionError(
            f"Unable to create isolated AirSim adapter for {key}"
        )
    set_active_robot = getattr(isolated, "set_active_robot", None)
    if callable(set_active_robot):
        set_active_robot(key)

    with _robot_adapters_lock:
        cached = _robot_adapters.get(key)
        if cached is not None and _adapter_is_connected(cached):
            invalidate = getattr(isolated, "invalidate_connection", None)
            if callable(invalidate):
                invalidate()
            return cached
        _robot_adapters[key] = isolated
    logger.info(
        "Created isolated AirSim execution adapter: robot=%s vehicle=%s",
        key,
        vehicle_name,
    )
    return isolated


@contextmanager
def robot_adapter_context(robot_id: str):
    """Bind get_adapter() to one robot for the current execution thread."""
    previous = getattr(_adapter_context, "robot_id", None)
    _adapter_context.robot_id = str(robot_id or "UAV_1")
    try:
        yield get_adapter()
    finally:
        if previous is None:
            try:
                delattr(_adapter_context, "robot_id")
            except AttributeError:
                pass
        else:
            _adapter_context.robot_id = previous


def get_all_adapters() -> list[SimAdapter]:
    """Return the primary adapter and all connected execution adapters."""
    with _robot_adapters_lock:
        adapters = [_adapter] if _adapter is not None else []
        adapters.extend(_robot_adapters.values())
        return adapters


def _close_robot_adapters() -> None:
    with _robot_adapters_lock:
        adapters = list(_robot_adapters.values())
        _robot_adapters.clear()
    for adapter in adapters:
        try:
            invalidate = getattr(adapter, "invalidate_connection", None)
            if callable(invalidate):
                invalidate()
            else:
                adapter.disconnect()
        except Exception:
            logger.debug("Failed to close isolated adapter", exc_info=True)


def switch_adapter(adapter_type: str, connection_str: str = "", timeout: float = 15.0) -> bool:
    """
    运行时切换适配器（先断开旧的再连新的）。
    """
    global _adapter
    _close_robot_adapters()
    if _adapter and _adapter.is_connected():
        _adapter.disconnect()
    return init_adapter(adapter_type, connection_str, timeout)
