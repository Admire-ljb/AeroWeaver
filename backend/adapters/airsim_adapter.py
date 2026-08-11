"""
airsim_adapter.py
OpenFly 定制版 AirSim 适配器

坐标系（直接 RPC 测量确认）：
  x_val=North, y_val=East, z_val: z减小=向上，z增大=向下（用户实测确认）
  spawn_z ≈ 2.251（无人机出生点，非零）

关键发现：moveToPosition/moveByVelocity/takeoff_async_join 全部不可用
唯一移动方式：simSetVehiclePose（瞬间传送）

坐标换算：
  向上altitude m: target_z = spawn_z - altitude (z减小=向上)
  fly_to_ned: airsim_z = spawn_z + down (NED down负 → z减小=向上)
"""
import logging
import os
import re
import time
import threading
from typing import Optional

from adapters.sim_adapter import (
    SimAdapter, Position, GPSPosition, VehicleState, ActionResult,
)

logger = logging.getLogger(__name__)

_AIR_THRESHOLD = 1.0  # 离地超过1m才算在空中
_BOTTOM_DISTANCE_SENSOR = os.getenv("AIRSIM_BOTTOM_DISTANCE_SENSOR", "BottomDistance")
_ACTIVATION_DROP_HEIGHT_M = float(os.getenv("AIRSIM_ACTIVATION_DROP_HEIGHT", "30"))
_HOVER_CLEARANCE_M = min(
    max(float(os.getenv("AIRSIM_HOVER_CLEARANCE", "4.0")), 1.0),
    4.8,
)
_ACTIVATION_DESCENT_SPEED_MPS = max(
    float(os.getenv("AIRSIM_ACTIVATION_DESCENT_SPEED", "6.0")),
    1.0,
)
_MIN_FLIGHT_CLEARANCE_M = max(
    float(os.getenv("AIRSIM_MIN_FLIGHT_CLEARANCE", "5.0")),
    3.0,
)
_CRUISE_CLEARANCE_M = max(
    float(os.getenv("AIRSIM_CRUISE_CLEARANCE", "8.0")),
    _MIN_FLIGHT_CLEARANCE_M + 1.0,
)
_FORMATION_STEP_INTERVAL_S = min(
    max(float(os.getenv("AIRSIM_FORMATION_STEP_INTERVAL", "0.05")), 0.04),
    0.10,
)
_FORMATION_GROUND_CHECK_STEPS = max(
    int(os.getenv("AIRSIM_FORMATION_GROUND_CHECK_STEPS", "10")),
    4,
)
_FORMATION_DEPTH_CHECK_STEPS = max(
    int(os.getenv("AIRSIM_FORMATION_DEPTH_CHECK_STEPS", "10")),
    5,
)


class AirSimAdapter(SimAdapter):
    name = "airsim_openfly"
    description = "AirSim multi-UAV global-pose control"
    supported_vehicles = ["multirotor"]

    def __init__(self, vehicle_name: str = None):
        self._vehicle_name = vehicle_name or os.getenv("AIRSIM_VEHICLE_NAME", "Drone_1")
        self._airsim_host = "127.0.0.1"
        self._airsim_port = 41451
        self._client = None
        self._connected = False
        self._active_robot = "UAV_1"
        self._vehicle_names = []
        self._vehicle_spawn_poses = {}
        self._vehicle_home_positions = {}
        self._spawn_z: float = 0.0
        self._spawn_x: float = 0.0
        self._spawn_y: float = 0.0
        self._home_position: Optional[Position] = None
        self.is_flying: bool = False  # 飞行中标记，摄像头流可据此降频
        self._stop_requested: bool = False  # 外部打断标志
        self._last_obstacle_info: dict = {}  # 最近一次避障信息
        self._landed: bool = False  # 已着陆标记（land 成功后置 True，takeoff 后置 False）
        self._hold_thread: Optional[threading.Thread] = None
        self._hold_running = False
        self._hold_lock = threading.Lock()
        self._hold_client = None
        self._hold_x: float = 0.0
        self._hold_y: float = 0.0
        self._hold_z: float = 0.0
        self._manual_lock = threading.Lock()
        self._manual_states = {}
        self._autonomous_vehicle: Optional[str] = None
        self._pool_active_robots = set()
        self._ground_clearance = {}

    def _raw(self) -> dict:
        try:
            return self._client.get_multirotor_state(self._vehicle_name) or {}
        except Exception as e:
            logger.warning(f"get_multirotor_state error: {e}")
            return {}

    @staticmethod
    def _vehicle_sort_key(name: str):
        match = re.search(r"(\d+)$", str(name or ""))
        return (int(match.group(1)) if match else 10**9, str(name or ""))

    @staticmethod
    def _position_from_pose(pose: dict):
        position = (pose or {}).get("position", {})
        return (
            float(position.get("x_val", 0.0)),
            float(position.get("y_val", 0.0)),
            float(position.get("z_val", 0.0)),
        )

    def _read_global_pose(self, vehicle_name: str = None):
        name = vehicle_name or self._vehicle_name
        try:
            return self._position_from_pose(self._client.sim_get_object_pose(name))
        except Exception as exc:
            logger.debug("simGetObjectPose failed for %s: %s", name, exc)
            raw = self._client.get_multirotor_state(name) or {}
            local = self._position_from_pose(raw.get("kinematics_estimated", {}))
            origin = self._vehicle_spawn_poses.get(name, (0.0, 0.0, 0.0))
            return tuple(local[index] + origin[index] for index in range(3))

    def _infer_vehicle_origin(self, vehicle_name: str):
        """Infer the fixed vehicle-local frame origin from global and local poses."""
        global_position = self._position_from_pose(self._client.sim_get_object_pose(vehicle_name))
        raw = self._client.get_multirotor_state(vehicle_name) or {}
        local_position = self._position_from_pose(raw.get("kinematics_estimated", {}))
        return tuple(
            global_position[index] - local_position[index]
            for index in range(3)
        )

    def vehicle_for_robot(self, robot_id: str = None) -> str:
        text = str(robot_id or self._active_robot or "UAV_1")
        if text in self._vehicle_names:
            return text
        match = re.search(r"(\d+)$", text)
        index = max(0, int(match.group(1)) - 1) if match else 0
        if index < len(self._vehicle_names):
            return self._vehicle_names[index]
        if match:
            return f"Drone_{index + 1}"
        return self._vehicle_name

    def set_active_robot(self, robot_id: str):
        vehicle_name = self.vehicle_for_robot(robot_id)
        if vehicle_name == self._vehicle_name and str(robot_id or "") == self._active_robot:
            return

        self._stop_hold()
        self._active_robot = str(robot_id or "UAV_1")
        self._vehicle_name = vehicle_name
        spawn = self._vehicle_spawn_poses.get(vehicle_name, (0.0, 0.0, 0.0))
        self._spawn_x, self._spawn_y, self._spawn_z = spawn
        self._home_position = self._vehicle_home_positions.get(
            vehicle_name,
            Position(north=spawn[0], east=spawn[1], down=spawn[2]),
        )
        self._hold_x, self._hold_y, self._hold_z = self._read_global_pose(vehicle_name)
        clearance = self.get_ground_clearance(vehicle_name)
        self._landed = clearance is not None and clearance <= _AIR_THRESHOLD
        logger.info(
            "Active robot %s routed to %s at global pose (%.2f, %.2f, %.2f)",
            self._active_robot,
            self._vehicle_name,
            self._hold_x,
            self._hold_y,
            self._hold_z,
        )

    def get_active_robot(self) -> str:
        return self._active_robot

    def invalidate_connection(self):
        """Drop stale RPC sockets without sending shutdown commands to AirSim."""
        self._stop_hold()
        for client in (self._hold_client, self._client):
            if client:
                try:
                    client.close()
                except Exception:
                    pass
        self._hold_client = None
        self._client = None
        self._connected = False
        with self._manual_lock:
            self._manual_states.clear()

    def _xyz(self):
        # Hold coordinates and public state both use the global NED frame.
        if self._hold_running:
            return (self._hold_x, self._hold_y, self._hold_z)
        return self._read_global_pose()

    def _set_pose(self, x, y, z):
        """传送到目标位置并持续维持（后台线程每50ms重设一次，对抗物理引擎）。"""
        with self._hold_lock:
            self._hold_x, self._hold_y, self._hold_z = float(x), float(y), float(z)
            # 立即设一次
            self._do_set_pose(x, y, z)
            # 如果 hold 线程没在跑（或者崩了），启动新的
            if not self._hold_running or (self._hold_thread and not self._hold_thread.is_alive()):
                self._hold_running = True
                self._hold_thread = threading.Thread(target=self._hold_loop, daemon=True)
                self._hold_thread.start()

    def _do_set_pose(self, x, y, z):
        """Set a global pose after converting it to the vehicle-local NED frame."""
        import math
        yaw = getattr(self, '_fly_yaw', 0.0)
        qw = math.cos(yaw / 2)
        qz = math.sin(yaw / 2)

        pose = {
            "position": {
                "x_val": float(x) - self._spawn_x,
                "y_val": float(y) - self._spawn_y,
                "z_val": float(z) - self._spawn_z,
            },
            "orientation": {"w_val": qw, "x_val": 0.0, "y_val": 0.0, "z_val": qz},
        }
        client = self._hold_client or self._client
        try:
            client._rpc.call("simSetVehiclePose", pose, True, self._vehicle_name)
        except Exception:
            pass

    def _set_vehicle_global_pose(self, vehicle_name: str, x: float, y: float, z: float, yaw: float = 0.0):
        """Set one explicitly named vehicle without mutating the adapter's active UAV."""
        import math

        spawn = self._vehicle_spawn_poses.get(vehicle_name)
        if spawn is None:
            raise RuntimeError(f"Unknown AirSim vehicle: {vehicle_name}")
        pose = {
            "position": {
                "x_val": float(x) - spawn[0],
                "y_val": float(y) - spawn[1],
                "z_val": float(z) - spawn[2],
            },
            "orientation": {
                "w_val": math.cos(float(yaw) / 2.0),
                "x_val": 0.0,
                "y_val": 0.0,
                "z_val": math.sin(float(yaw) / 2.0),
            },
        }
        client = self._hold_client or self._client
        if not client:
            raise RuntimeError("AirSim RPC client unavailable")
        client._rpc.call("simSetVehiclePose", pose, True, vehicle_name)

    def reset_robot_pose(self, robot_id: str, position: list, *, in_air: bool = True) -> ActionResult:
        """Set one pooled AirSim vehicle pose during pre-mission initialization."""
        values = list(position or [])
        if len(values) < 3:
            return ActionResult(False, "position requires three coordinates")
        vehicle_name = self.vehicle_for_robot(robot_id)
        try:
            self._stop_hold()
            target = [float(values[0]), float(values[1]), float(values[2])]
            self._set_vehicle_global_pose(vehicle_name, *target)
            if str(robot_id) == self._active_robot:
                self._hold_x, self._hold_y, self._hold_z = target
                self._landed = not bool(in_air)
            with self._manual_lock:
                self._manual_states.pop(vehicle_name, None)
            return ActionResult(
                True,
                "AirSim scene start pose initialized",
                {"position": target, "vehicle": vehicle_name},
            )
        except Exception as exc:
            logger.exception("AirSim scene reset failed for %s", robot_id)
            return ActionResult(False, f"AirSim scene reset failed: {exc}")

    def apply_vehicle_pool_layout(self, layout: list[dict]) -> ActionResult:
        """Activate or park all pre-spawned UAVs without restarting Unreal."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")

        self._stop_hold()
        active_items = [item for item in layout if item.get("active")]
        active_robots = [str(item.get("robot_id") or "") for item in active_items]
        newly_active = [
            item
            for item in active_items
            if str(item.get("robot_id") or "") not in self._pool_active_robots
        ]
        try:
            with self._manual_lock:
                self._manual_states.clear()
                for item in layout:
                    vehicle_name = str(item.get("vehicle") or "")
                    position = item.get("position") or []
                    if vehicle_name not in self._vehicle_names or len(position) < 3:
                        raise RuntimeError(f"Invalid pool entry for {vehicle_name or 'unknown vehicle'}")
                    target_z = float(position[2])
                    if item in newly_active:
                        target_z -= _ACTIVATION_DROP_HEIGHT_M
                    self._set_vehicle_global_pose(
                        vehicle_name,
                        float(position[0]),
                        float(position[1]),
                        target_z,
                    )

            descent = self._descend_to_hover_clearance(newly_active)
            if not descent.success:
                return descent

            for item in active_items:
                vehicle_name = str(item.get("vehicle") or "")
                x, y, z = self._read_global_pose(vehicle_name)
                self._vehicle_home_positions[vehicle_name] = Position(
                    north=x,
                    east=y,
                    down=z,
                )

            if self._active_robot not in active_robots and active_robots:
                self.set_active_robot(active_robots[0])
            elif self._vehicle_name in self._vehicle_home_positions:
                self._home_position = self._vehicle_home_positions[self._vehicle_name]
            self._pool_active_robots = set(active_robots)
            return ActionResult(
                success=True,
                message=(
                    f"Applied AirSim pool layout: {len(active_robots)} active / "
                    f"{len(layout)} total; {len(newly_active)} descended"
                ),
                data={
                    "active": active_robots,
                    "newly_active": [
                        str(item.get("robot_id") or "") for item in newly_active
                    ],
                    "ground_clearance": dict(self._ground_clearance),
                    "hover_clearance": _HOVER_CLEARANCE_M,
                },
            )
        except Exception as exc:
            logger.warning("AirSim pool layout failed: %s", exc)
            return ActionResult(success=False, message=str(exc))

    def get_ground_clearance(self, vehicle_name: str = None) -> Optional[float]:
        """Return the real downward distance-sensor reading in metres."""
        vehicle = vehicle_name or self._vehicle_name
        client = self._hold_client or self._client
        if not client:
            return None
        try:
            data = client.get_distance_sensor_data(
                _BOTTOM_DISTANCE_SENSOR,
                vehicle,
            ) or {}
            distance = float(data.get("distance"))
            max_distance = float(data.get("max_distance", 100.0) or 100.0)
            # AirSim reports a value just below max_distance when the ray misses
            # every surface (for example, when a vehicle is below the landscape).
            # Treat that sentinel-like reading as invalid so recovery can climb
            # until the bottom ray intersects the terrain again.
            if not (0.0 < distance < max_distance * 0.95):
                return None
            distance = round(distance, 3)
            self._ground_clearance[vehicle] = distance
            return distance
        except (TypeError, ValueError, RuntimeError, AttributeError):
            return None

    def _get_altitude(self) -> float:
        """Return height above the local surface, not absolute world Z."""
        clearance = self.get_ground_clearance(self._vehicle_name)
        if clearance is not None:
            return clearance
        _, _, z = self._xyz()
        return abs(z)

    def _recover_and_ensure_clearance(
        self,
        minimum: float = _MIN_FLIGHT_CLEARANCE_M,
    ) -> Optional[float]:
        """Climb out of terrain and establish a valid minimum AGL."""
        x, y, z = self._xyz()
        clearance = self.get_ground_clearance(self._vehicle_name)
        if clearance is None:
            logger.warning(
                "%s has no valid bottom range; climbing to recover",
                self._vehicle_name,
            )
            for _ in range(40):
                z -= 2.0
                self._hold_x, self._hold_y, self._hold_z = x, y, z
                self._do_set_pose(x, y, z)
                time.sleep(0.08)
                clearance = self.get_ground_clearance(self._vehicle_name)
                if clearance is not None:
                    break
        if clearance is None:
            return None
        if clearance < minimum:
            climb = minimum - clearance + 0.3
            z -= climb
            self._hold_x, self._hold_y, self._hold_z = x, y, z
            self._do_set_pose(x, y, z)
            time.sleep(0.12)
            clearance = self.get_ground_clearance(self._vehicle_name) or minimum
        return clearance

    def _distance_with_retry(self, vehicle_name: str) -> Optional[float]:
        for _ in range(10):
            distance = self.get_ground_clearance(vehicle_name)
            if distance is not None:
                return distance
            time.sleep(0.12)
        return None

    def _descend_to_hover_clearance(self, items: list[dict]) -> ActionResult:
        """Animate multiple UAVs downward until their bottom sensors read 4 m."""
        if not items:
            return ActionResult(success=True, message="No newly active UAVs")

        states = []
        for item in items:
            vehicle = str(item.get("vehicle") or "")
            x, y, z = self._read_global_pose(vehicle)
            distance = self._distance_with_retry(vehicle)
            if distance is None:
                return ActionResult(
                    success=False,
                    message=f"{vehicle} bottom distance sensor is not returning valid data",
                )
            states.append({
                "robot_id": str(item.get("robot_id") or ""),
                "vehicle": vehicle,
                "x": x,
                "y": y,
                "z": z,
                "target_z": z + distance - _HOVER_CLEARANCE_M,
            })

        step_seconds = 0.12
        max_step = _ACTIVATION_DESCENT_SPEED_MPS * step_seconds
        while True:
            moving = False
            for item in states:
                remaining = item["target_z"] - item["z"]
                if abs(remaining) <= 0.04:
                    continue
                moving = True
                item["z"] += max(-max_step, min(max_step, remaining))
                self._set_vehicle_global_pose(
                    item["vehicle"],
                    item["x"],
                    item["y"],
                    item["z"],
                )
            if not moving:
                break
            time.sleep(step_seconds)

        clearances = {}
        for item in states:
            measured = self._distance_with_retry(item["vehicle"])
            if measured is None:
                return ActionResult(
                    success=False,
                    message=f"{item['vehicle']} lost bottom distance data during descent",
                )
            correction = measured - _HOVER_CLEARANCE_M
            if abs(correction) > 0.08:
                item["z"] += correction
                self._set_vehicle_global_pose(
                    item["vehicle"],
                    item["x"],
                    item["y"],
                    item["z"],
                )
                time.sleep(0.08)
                measured = self._distance_with_retry(item["vehicle"]) or measured
            clearances[item["robot_id"]] = round(measured, 2)
            logger.info(
                "%s/%s activation descent complete: clearance=%.2fm",
                item["robot_id"],
                item["vehicle"],
                measured,
            )

        return ActionResult(
            success=True,
            message=f"{len(states)} UAV(s) hovering near {_HOVER_CLEARANCE_M:.1f} m AGL",
            data={"ground_clearance": clearances},
        )

    def settle_active_fleet(self, count: int) -> ActionResult:
        """Settle the first N vehicles after an independent AirSim restart."""
        active_items = []
        for index, vehicle in enumerate(self._vehicle_names[:max(0, int(count))], start=1):
            active_items.append({
                "robot_id": f"UAV_{index}",
                "vehicle": vehicle,
                "active": True,
            })
        result = self._descend_to_hover_clearance(active_items)
        if result.success:
            self._pool_active_robots = {
                str(item["robot_id"]) for item in active_items
            }
            for item in active_items:
                x, y, z = self._read_global_pose(item["vehicle"])
                self._vehicle_home_positions[item["vehicle"]] = Position(
                    north=x,
                    east=y,
                    down=z,
                )
            if self._vehicle_name in self._vehicle_home_positions:
                self._home_position = self._vehicle_home_positions[
                    self._vehicle_name
                ]
        return result

    def _check_obstacle(self, x, y, z):
        """射线碰撞检测：当前位置到目标点之间是否有障碍物。"""
        client = self._hold_client or self._client
        try:
            # simTestLineOfSightToPoint 返回 True=可见(无障碍), False=被遮挡(有障碍)
            visible = client._rpc.call("simTestLineOfSightToPoint",
                {"x_val": float(x), "y_val": float(y), "z_val": float(z)}, self._vehicle_name)
            return not visible  # True=有障碍物
        except Exception:
            return False  # API 失败时不阻断飞行

    def _check_collision(self):
        """检查当前是否发生碰撞。"""
        client = self._hold_client or self._client
        try:
            col = client._rpc.call("simGetCollisionInfo", self._vehicle_name)
            return col.get("has_collided", False)
        except Exception:
            return False

    def _fly_smooth(self, tx, ty, tz, speed=8.0):
        """
        安全飞行：保持当前高度水平飞到目标上方，再调整高度。
        返回: 'ok' / 'obstacle' / 'stopped'
        """
        import math

        sx, sy, sz = self._hold_x, self._hold_y, self._hold_z
        dx, dy, dz = tx - sx, ty - sy, tz - sz
        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
        if dist < 0.1:
            self._hold_x, self._hold_y, self._hold_z = tx, ty, tz
            return 'ok'

        h_dist = math.sqrt(dx*dx + dy*dy)

        if h_dist < 3.0:
            return self._fly_smooth_raw(tx, ty, tz, speed)
        else:
            fly_z = min(sz, tz)
            logger.info(f"🛫 飞行: 水平{h_dist:.0f}m, 高度{-fly_z:.0f}m")
            if abs(sz - fly_z) > 1.0:
                result = self._fly_smooth_raw(sx, sy, fly_z, speed)
                if result != 'ok':
                    return result
            result = self._fly_smooth_raw(tx, ty, fly_z, speed)
            if result != 'ok':
                return result
            if abs(tz - fly_z) > 1.0:
                return self._fly_smooth_raw(tx, ty, tz, speed)
            return 'ok'

    def _fly_smooth_raw(
        self,
        tx,
        ty,
        tz,
        speed=8.0,
        step_interval=0.033,
        ground_check_interval=3,
        depth_check_interval=15,
    ):
        """底层插值飞行 + 实时 LiDAR/深度避障 + 外部打断支持。

        飞行中每 15 步（~500ms）检查前方深度图：
        - 前方 < SAFE_DIST 米 → 立即停下，返回 'obstacle'
        - 外部 stop_event 置位 → 立即停下，返回 'stopped'
        - 正常到达 → 返回 'ok'
        """
        import math
        SAFE_DIST = 8.0      # 前方安全距离（米），低于此距离停下
        CHECK_INTERVAL = 15  # 每 15 步检查一次（~500ms）
        GROUND_CHECK_INTERVAL = 3

        CHECK_INTERVAL = max(int(depth_check_interval), 1)
        GROUND_CHECK_INTERVAL = max(int(ground_check_interval), 1)
        self.is_flying = True
        sx, sy, sz = self._hold_x, self._hold_y, self._hold_z
        dx, dy, dz = tx - sx, ty - sy, tz - sz
        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
        if dist < 0.1:
            self._hold_x, self._hold_y, self._hold_z = tx, ty, tz
            self.is_flying = False
            return 'ok'

        # 朝向对准运动方向
        if abs(dx) > 0.1 or abs(dy) > 0.1:
            self._fly_yaw = math.atan2(dy, dx)

        duration = dist / speed
        step_interval = max(float(step_interval), 0.02)
        steps = max(1, int(duration / step_interval))
        ground_safety_offset = 0.0
        missing_ground_checks = 0

        for i in range(1, steps + 1):
            # ── 外部打断检查 ──
            if self._stop_requested:
                logger.warning("🛑 外部打断！停止飞行")
                self.is_flying = False
                self._stop_requested = False
                return 'stopped'

            t = i / steps
            nx = sx + dx * t
            ny = sy + dy * t
            nz = sz + dz * t + ground_safety_offset
            self._hold_x, self._hold_y, self._hold_z = nx, ny, nz
            self._do_set_pose(nx, ny, nz)
            time.sleep(step_interval)

            if i % GROUND_CHECK_INTERVAL == 0:
                clearance = self.get_ground_clearance(self._vehicle_name)
                if clearance is None:
                    missing_ground_checks += 1
                    if missing_ground_checks >= 2:
                        climb = 2.0
                        ground_safety_offset -= climb
                        nz -= climb
                        self._hold_z = nz
                        self._do_set_pose(nx, ny, nz)
                        logger.warning(
                            "Ground range lost during flight; climbing %.2fm",
                            climb,
                        )
                else:
                    missing_ground_checks = 0
                if clearance is not None and clearance < _MIN_FLIGHT_CLEARANCE_M:
                    climb = _MIN_FLIGHT_CLEARANCE_M - clearance + 0.3
                    ground_safety_offset -= climb
                    nz -= climb
                    self._hold_z = nz
                    self._do_set_pose(nx, ny, nz)
                    logger.warning(
                        "Ground-clearance guard: %.2fm -> climbing %.2fm",
                        clearance,
                        climb,
                    )

            # ── 深度图避障（每 CHECK_INTERVAL 步） ──
            if i % CHECK_INTERVAL == 0:
                v_move = abs(nz - sz) / max(dist, 0.1)  # 垂直分量占比
                going_down = (nz > sz)  # z增大=向下
                going_up = (nz < sz)    # z减小=向上

                # 向上飞时不检查避障（向上是逃脱障碍的方式）
                if v_move > 0.7 and going_up:
                    pass  # 跳过避障检查
                elif v_move > 0.7 and going_down:
                    # 向下飞 → 检查下方
                    front_dist = self._check_depth('cam_down')
                    if front_dist is not None and front_dist < SAFE_DIST:
                        logger.warning(f"⚠️ 下方障碍物 {front_dist:.1f}m！自动悬停")
                        self.is_flying = False
                        self._last_obstacle_info = {'front_dist': front_dist, 'direction': '下方', 'position': {'x': nx, 'y': ny, 'z': nz}, 'target': {'x': tx, 'y': ty, 'z': tz}}
                        return 'obstacle'
                else:
                    # 水平飞 → 检查前方
                    front_dist = self._check_depth('cam_front')
                    if front_dist is not None and front_dist < SAFE_DIST:
                        logger.warning(f"⚠️ 前方障碍物 {front_dist:.1f}m！自动悬停")
                        self.is_flying = False
                        self._last_obstacle_info = {'front_dist': front_dist, 'direction': '前方', 'position': {'x': nx, 'y': ny, 'z': nz}, 'target': {'x': tx, 'y': ty, 'z': tz}}
                        return 'obstacle'

        effective_tz = tz + ground_safety_offset
        self._hold_x, self._hold_y, self._hold_z = tx, ty, effective_tz
        self._last_effective_target = (tx, ty, effective_tz)
        self.is_flying = False
        return 'ok'

    def _check_depth(self, camera_name: str = None) -> float:
        """用深度摄像头检查指定方向最近障碍距离（米）。返回 None 表示检查失败。"""
        try:
            resp = self._client.sim_get_images([{
                'camera_name': camera_name,
                'image_type': 2,  # DepthPerspective
                'pixels_as_float': True,
                'compress': False,
            }], vehicle_name=self._vehicle_name)
            if not resp:
                return None
            r = resp[0]
            h, w = r.get('height', 0), r.get('width', 0)
            data = r.get('image_data_float') or []
            if not data or h == 0 or w == 0:
                return None

            import struct
            if isinstance(data, bytes):
                data = list(struct.unpack(f'{len(data)//4}f', data))

            # 取中心区域（中间 1/3 x 中间 1/3）的最小深度
            h3, w3 = h // 3, w // 3
            min_depth = 999.0
            for row in range(h3, h3 * 2):
                row_start = row * w + w3
                row_end = row_start + w3
                if row_end <= len(data):
                    for d in data[row_start:row_end]:
                        if 0.1 < d < min_depth:
                            min_depth = d
            return min_depth if min_depth < 999.0 else None
        except Exception as e:
            return None

    def request_stop(self):
        """外部请求停止飞行（用户打断/安全包线）。"""
        self._stop_requested = True

    def _hold_loop(self):
        """后台线程：每33ms重设位置(~30fps)。simSetVehiclePose 覆盖物理引擎。"""
        client = self._hold_client or self._client
        try:
            while self._hold_running:
                self._do_set_pose(self._hold_x, self._hold_y, self._hold_z)
                import time as _t; _t.sleep(0.033)
        except Exception as e:
            logger.warning(f"Hold thread error: {e}")
        finally:
            self._hold_running = False

    def _stop_hold(self):
        """停止 hold 线程。"""
        self._hold_running = False
        if self._hold_thread:
            self._hold_thread.join(timeout=1)
            self._hold_thread = None

    def connect(self, connection_str: str = "", timeout: float = 15.0) -> bool:
        ip, port = "127.0.0.1", 41451
        if connection_str:
            parts = connection_str.split(":")
            ip = parts[0]
            if len(parts) > 1:
                port = int(parts[1])
        self._airsim_host = ip
        self._airsim_port = port
        try:
            from adapters.airsim_rpc import AirSimDirectClient
            self._client = AirSimDirectClient(ip, port, timeout=timeout)
            if not self._client.connect():
                raise ConnectionError(f"Cannot connect to {ip}:{port}")
            if not self._client.ping():
                raise ConnectionError("ping failed")
            self._vehicle_names = sorted(
                [str(name) for name in self._client.list_vehicles() if str(name).strip()],
                key=self._vehicle_sort_key,
            )
            if not self._vehicle_names:
                self._vehicle_names = [self._vehicle_name]
            if self._vehicle_name not in self._vehicle_names:
                self._vehicle_name = self._vehicle_names[0]

            self._vehicle_spawn_poses = {
                name: self._infer_vehicle_origin(name)
                for name in self._vehicle_names
            }
            self._vehicle_home_positions = {
                name: Position(north=pose[0], east=pose[1], down=pose[2])
                for name, pose in self._vehicle_spawn_poses.items()
            }
            for name in self._vehicle_names:
                try:
                    self._client.enable_api_control(True, name)
                    self._client.arm_disarm(True, name)
                except Exception as exc:
                    logger.warning("AirSim control enable failed for %s: %s", name, exc)

            self._connected = True
            self._active_robot = f"UAV_{self._vehicle_names.index(self._vehicle_name) + 1}"
            self._spawn_x, self._spawn_y, self._spawn_z = self._vehicle_spawn_poses[self._vehicle_name]
            self._hold_x, self._hold_y, self._hold_z = self._read_global_pose()
            self._landed = self._hold_z >= -_AIR_THRESHOLD
            self._home_position = Position(
                north=self._spawn_x,
                east=self._spawn_y,
                down=self._spawn_z,
            )
            # 第二个 RPC 连接，专门给 hold 线程用（避免和摄像头/LiDAR 抢 socket）
            try:
                from adapters.airsim_rpc import AirSimDirectClient
                self._hold_client = AirSimDirectClient(ip, port, timeout=5)
                self._hold_client.connect()
                logger.info("Hold thread RPC connection established")
            except Exception as he:
                logger.warning(f"Hold RPC connect failed, sharing main: {he}")
                self._hold_client = self._client
            logger.info(
                "AirSim connected: %s:%s, vehicles=%s, active=%s",
                ip,
                port,
                self._vehicle_names,
                self._vehicle_name,
            )
            return True
        except Exception as e:
            logger.error(f"AirSim connect failed: {e}")
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._stop_hold()
        if self._hold_client and self._hold_client is not self._client:
            try:
                self._hold_client.close()
            except Exception:
                pass
            self._hold_client = None
        if self._client:
            for vehicle_name in self._vehicle_names or [self._vehicle_name]:
                try:
                    self._client.enable_api_control(False, vehicle_name)
                except Exception:
                    pass
            try:
                self._client.close()
            except Exception:
                pass
        self._connected = False
        self._client = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_state(self) -> Optional[VehicleState]:
        if not self._connected:
            return None
        try:
            x, y, z = self._xyz()
            clearance = self.get_ground_clearance(self._vehicle_name)
            in_air = (
                not self._landed
                and (clearance is None or clearance > _AIR_THRESHOLD)
            )
            return VehicleState(
                armed=True,
                in_air=in_air,
                position_ned=Position(north=x, east=y, down=z),
                battery_voltage=12.6,
                battery_percent=100.0,
                heading_deg=(
                    getattr(self, "_fly_yaw", 0.0)
                    * 180.0
                    / 3.141592653589793
                ) % 360.0,
                velocity=[0.0, 0.0, 0.0],
            )
        except Exception as e:
            logger.warning(f"get_state error: {e}")
            self.invalidate_connection()
            return None

    def get_position(self) -> Optional[Position]:
        s = self.get_state()
        return s.position_ned if s else None

    def get_motion_position(self) -> Position:
        """Return the controller's current global pose without an extra sensor RPC."""
        north, east, down = self._xyz()
        return Position(north=north, east=east, down=down)

    def get_gps(self) -> Optional[GPSPosition]:
        if not self._connected:
            return None
        try:
            payload = self._client.get_gps_data("", self._vehicle_name) or {}
            gnss = payload.get("gnss", {}) or {}
            point = (
                gnss.get("geo_point")
                or payload.get("geo_point")
                or payload.get("gps_location")
                or {}
            )
            lat = float(point.get("latitude", point.get("lat", 0.0)) or 0.0)
            lon = float(point.get("longitude", point.get("lon", 0.0)) or 0.0)
            alt = float(point.get("altitude", point.get("alt", 0.0)) or 0.0)
            if lat or lon or alt:
                return GPSPosition(lat=lat, lon=lon, alt=alt)
        except Exception as exc:
            logger.debug("AirSim GPS sensor unavailable: %s", exc)

        try:
            point = (self._raw().get("gps_location") or {})
            return GPSPosition(
                lat=float(point.get("latitude", 0.0) or 0.0),
                lon=float(point.get("longitude", 0.0) or 0.0),
                alt=float(point.get("altitude", 0.0) or 0.0),
            )
        except Exception:
            return None

    def get_battery(self) -> tuple:
        # SimpleFlight exposes unlimited simulator power, not a battery model.
        return (12.6, 1.0)

    def get_sensor_snapshot(self, sensor_types=None) -> dict:
        """Read real sensor data for the selected AirSim vehicle."""
        if not self._connected:
            raise RuntimeError("Not connected")

        if isinstance(sensor_types, str):
            requested = {
                item.strip().lower()
                for item in sensor_types.split(",")
                if item.strip()
            }
        else:
            requested = {
                str(item).strip().lower()
                for item in (sensor_types or ["all"])
                if str(item).strip()
            }
        if not requested or "all" in requested:
            requested = {
                "imu",
                "gps",
                "barometer",
                "magnetometer",
                "distance",
                "lidar",
                "camera",
                "battery",
            }

        snapshot = {
            "source": "airsim",
            "vehicle": self._vehicle_name,
            "robot_id": self._active_robot,
            "timestamp": time.time(),
            "available": {},
        }

        def record(name, reader):
            try:
                value = reader()
                if value:
                    snapshot[f"{name}_data"] = value
                    snapshot["available"][name] = True
                else:
                    snapshot["available"][name] = False
            except Exception as exc:
                snapshot["available"][name] = False
                snapshot.setdefault("errors", {})[name] = str(exc)

        if "imu" in requested:
            record(
                "imu",
                lambda: self._client.get_imu_data("", self._vehicle_name),
            )

        if "gps" in requested:
            def read_gps():
                gps = self.get_gps()
                if gps is None:
                    return None
                return {
                    "latitude": gps.lat,
                    "longitude": gps.lon,
                    "altitude": gps.alt,
                }
            record("gps", read_gps)

        if "barometer" in requested:
            record(
                "barometer",
                lambda: self._client.get_barometer_data("", self._vehicle_name),
            )

        if "magnetometer" in requested:
            record(
                "magnetometer",
                lambda: self._client.get_magnetometer_data("", self._vehicle_name),
            )

        if "distance" in requested:
            def read_distance():
                distance = self.get_ground_clearance(self._vehicle_name)
                if distance is None:
                    return None
                return {
                    "sensor": _BOTTOM_DISTANCE_SENSOR,
                    "distance_m": distance,
                }
            record("distance", read_distance)

        if "lidar" in requested:
            def read_lidar():
                last_error = None
                for name in ("LidarSensor1", "Lidar1", "lidar"):
                    try:
                        data = self._client.get_lidar_data(
                            name,
                            self._vehicle_name,
                        ) or {}
                        points = data.get("point_cloud") or []
                        if len(points) < 3:
                            continue
                        distances = []
                        for index in range(0, len(points) - 2, 3):
                            px = float(points[index])
                            py = float(points[index + 1])
                            pz = float(points[index + 2])
                            distances.append(
                                (px * px + py * py + pz * pz) ** 0.5
                            )
                        return {
                            "sensor": name,
                            "point_count": len(points) // 3,
                            "min_distance_m": round(min(distances), 3),
                            "max_distance_m": round(max(distances), 3),
                            "timestamp": data.get(
                                "time_stamp",
                                data.get("timestamp"),
                            ),
                        }
                    except Exception as exc:
                        last_error = exc
                if last_error:
                    raise last_error
                return None
            record("lidar", read_lidar)

        if "camera" in requested:
            def read_camera():
                import base64
                encoded = self.get_image_base64("front")
                if not encoded:
                    return None
                return {
                    "camera": "front",
                    "encoding": "jpeg",
                    "bytes": len(base64.b64decode(encoded)),
                    "status": "active",
                }
            record("camera", read_camera)

        if "battery" in requested:
            voltage, remaining = self.get_battery()
            snapshot["battery_data"] = {
                "voltage_v": voltage,
                "remaining_percent": remaining * 100.0,
                "simulated_unlimited_power": True,
            }
            snapshot["available"]["battery"] = True

        snapshot["success_count"] = sum(
            bool(value) for value in snapshot["available"].values()
        )
        snapshot["requested_count"] = len(requested)
        return snapshot

    def get_image_base64(self, camera_name: str = None) -> str:
        """获取指定摄像头图像（base64 JPEG）。用摄像头专用连接避免和 hold 冲突。"""
        def _candidates(name):
            aliases = {
                "front": [os.getenv("AIRSIM_CAMERA_FRONT"), "0", "front_center", "front", "cam_front", "CameraFront", "FPV", "fpv"],
                "left": [os.getenv("AIRSIM_CAMERA_LEFT"), "2", "front_left", "left", "cam_left", "CameraLeft"],
                "right": [os.getenv("AIRSIM_CAMERA_RIGHT"), "1", "front_right", "right", "cam_right", "CameraRight"],
                "rear": [os.getenv("AIRSIM_CAMERA_REAR"), "4", "back_center", "rear", "back", "cam_rear", "cam_back", "CameraRear", "CameraBack"],
                "down": [os.getenv("AIRSIM_CAMERA_DOWN"), "3", "bottom_center", "bottom", "down", "cam_down", "CameraDown"],
            }
            key = str(name or "front").lower()
            if "left" in key:
                group = aliases["left"]
            elif "right" in key:
                group = aliases["right"]
            elif "rear" in key or "back" in key:
                group = aliases["rear"]
            elif "down" in key or "bottom" in key:
                group = aliases["down"]
            else:
                group = aliases["front"]
            seen, out = set(), []
            for item in [name, *group]:
                if item is None:
                    continue
                item = str(item).strip()
                if item and item not in seen:
                    seen.add(item)
                    out.append(item)
            return out

        try:
            import base64, cv2, numpy as np
            client = getattr(self, '_cam_client', None) or self._client
            last_error = None
            for cam in _candidates(camera_name):
                try:
                    responses = client.sim_get_images([{
                        'camera_name': cam,
                        'image_type': 0,
                        'pixels_as_float': False,
                        'compress': False,
                    }], vehicle_name=self._vehicle_name)
                    if responses:
                        r = responses[0]
                        h, w = r.get('height', 0), r.get('width', 0)
                        data = r.get('image_data_uint8') or r.get('image_data', b'')
                        if isinstance(data, str):
                            data = base64.b64decode(data)
                        elif isinstance(data, list):
                            data = bytes(data)
                        if h > 0 and w > 0 and len(data) >= h * w * 3:
                            img = np.frombuffer(data, dtype=np.uint8)[:h * w * 3].reshape(h, w, 3)
                            _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                            return base64.b64encode(buf.tobytes()).decode('ascii')
                except Exception as e:
                    last_error = e
            if last_error:
                logger.debug(f'get_image_base64 camera candidates failed: {last_error}')
        except Exception as e:
            logger.warning(f'get_image_base64 error: {e}')
        return None

    def is_armed(self) -> bool:
        return self._connected

    def is_in_air(self) -> bool:
        if self._landed:
            return False
        clearance = self.get_ground_clearance(self._vehicle_name)
        return clearance is None or clearance > _AIR_THRESHOLD

    def arm(self) -> ActionResult:
        try:
            self._client.arm_disarm(True, self._vehicle_name)
            return ActionResult(success=True, message="Armed")
        except Exception as e:
            return ActionResult(success=False, message=str(e))

    def disarm(self) -> ActionResult:
        try:
            self._client.arm_disarm(False, self._vehicle_name)
            return ActionResult(success=True, message="Disarmed")
        except Exception as e:
            return ActionResult(success=False, message=str(e))

    def takeoff(self, altitude: float = 5.0) -> ActionResult:
        """Climb by a relative height and report the real final AGL."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        try:
            altitude = min(max(float(altitude), 0.5), 100.0)
            x, y, z0 = self._xyz()
            clearance = self.get_ground_clearance(self._vehicle_name)
            if clearance is None:
                clearance = self._recover_and_ensure_clearance(
                    _MIN_FLIGHT_CLEARANCE_M,
                )
                x, y, z0 = self._xyz()
            if clearance is None:
                return ActionResult(
                    success=False,
                    message="Bottom distance sensor unavailable; takeoff aborted",
                )

            target_z = z0 - altitude
            logger.info(
                "Takeoff: AGL %.2fm + %.2fm -> target z %.2f",
                clearance,
                altitude,
                target_z,
            )
            self._landed = False
            if not self._hold_running:
                self._set_pose(x, y, z0)
            flight = self._fly_smooth_raw(x, y, target_z, speed=5.0)
            if flight != "ok":
                return ActionResult(
                    success=False,
                    message=f"Takeoff stopped: {flight}",
                )
            final_clearance = self.get_ground_clearance(self._vehicle_name)
            if final_clearance is None:
                return ActionResult(
                    success=False,
                    message="Takeoff completed but final AGL could not be verified",
                )
            return ActionResult(
                success=True,
                message=f"Takeoff OK: AGL={final_clearance:.2f}m",
                data={
                    "altitude": round(final_clearance, 3),
                    "climb_m": altitude,
                    "position": list(self._xyz()),
                },
            )
        except Exception as e:
            return ActionResult(success=False, message=str(e))

    def land(self) -> ActionResult:
        """Descend against the real bottom range without the cruise guard."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        try:
            touchdown_clearance = 0.8
            x, y, z = self._xyz()
            if not self._hold_running:
                self._set_pose(x, y, z)

            clearance = self.get_ground_clearance(self._vehicle_name)
            if clearance is None:
                clearance = self._recover_and_ensure_clearance(
                    _MIN_FLIGHT_CLEARANCE_M,
                )
            if clearance is None:
                return ActionResult(
                    success=False,
                    message="Bottom distance sensor unavailable; landing aborted",
                )

            for _ in range(240):
                if self._stop_requested:
                    self._stop_requested = False
                    return ActionResult(
                        success=False,
                        message=f"Landing aborted at AGL={clearance:.2f}m",
                    )

                clearance = self.get_ground_clearance(self._vehicle_name)
                if clearance is None:
                    return ActionResult(
                        success=False,
                        message="Lost bottom range during landing; holding position",
                    )
                if clearance <= touchdown_clearance + 0.05:
                    break

                step = min(
                    max((clearance - touchdown_clearance) * 0.35, 0.08),
                    1.0,
                )
                x, y, z = self._xyz()
                z += min(step, clearance - touchdown_clearance)
                self._hold_x, self._hold_y, self._hold_z = x, y, z
                self._do_set_pose(x, y, z)
                time.sleep(0.08)

            final_clearance = (
                self.get_ground_clearance(self._vehicle_name)
                or touchdown_clearance
            )
            self._landed = True
            return ActionResult(
                success=True,
                message=f"Landed: AGL={final_clearance:.2f}m",
                data={
                    "ground_clearance": round(final_clearance, 3),
                    "position": list(self._xyz()),
                },
            )
        except Exception as e:
            return ActionResult(success=False, message=str(e))

    def fly_to_ned(self, north: float, east: float, down: float,
                   speed: float = 8.0) -> ActionResult:
        """Fly to a map coordinate while enforcing local ground clearance."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        control_vehicle = self._vehicle_name
        self._autonomous_vehicle = control_vehicle
        with self._manual_lock:
            self._manual_states.pop(control_vehicle, None)
        try:
            abs_x = float(north)
            abs_y = float(east)
            requested_z = float(down)
            logger.info(
                "fly_to_ned: global target (%.1f, %.1f, %.1f) for %s",
                abs_x,
                abs_y,
                requested_z,
                self._vehicle_name,
            )
            if not self._hold_running:
                self._set_pose(self._hold_x, self._hold_y, self._hold_z)

            clearance = self._recover_and_ensure_clearance(
                _MIN_FLIGHT_CLEARANCE_M,
            )
            if clearance is None:
                return ActionResult(
                    success=False,
                    message="Bottom distance sensor unavailable; refusing unsafe map flight",
                )

            start_x, start_y, start_z = self._xyz()
            cruise_z = start_z
            if clearance < _CRUISE_CLEARANCE_M:
                cruise_z -= _CRUISE_CLEARANCE_M - clearance
                result = self._fly_smooth_raw(
                    start_x,
                    start_y,
                    cruise_z,
                    speed=max(float(speed), 4.0),
                )
                if result != "ok":
                    return ActionResult(success=False, message=f"Unable to establish cruise height: {result}")

            result = self._fly_smooth_raw(
                abs_x,
                abs_y,
                self._hold_z,
                speed=speed,
            )
            if result == 'obstacle':
                info = self._last_obstacle_info
                direction = info.get('direction', '前方')
                dist_val = info.get('front_dist', 0)
                return ActionResult(
                    success=False,
                    message=f"⚠️ {direction}{dist_val:.1f}m处检测到障碍物，已自动悬停。请重新规划航线或改变方向。"
                )
            if result == 'stopped':
                return ActionResult(success=False, message="飞行被外部打断，已悬停。")

            destination_clearance = self.get_ground_clearance(self._vehicle_name)
            if destination_clearance is None:
                return ActionResult(
                    success=False,
                    message="Destination ground range unavailable; hovering at cruise height",
                )
            _, _, destination_z = self._xyz()
            local_ground_z = destination_z + destination_clearance
            lowest_safe_z = local_ground_z - _MIN_FLIGHT_CLEARANCE_M
            effective_target_z = min(requested_z, lowest_safe_z)
            adjusted = effective_target_z < requested_z - 0.05

            result = self._fly_smooth_raw(
                abs_x,
                abs_y,
                effective_target_z,
                speed=max(min(float(speed), 8.0), 2.0),
            )
            if result != "ok":
                return ActionResult(
                    success=False,
                    message=f"Final altitude adjustment stopped: {result}",
                )

            final_clearance = self._recover_and_ensure_clearance(
                _MIN_FLIGHT_CLEARANCE_M,
            )
            if final_clearance is None:
                return ActionResult(
                    success=False,
                    message="Unable to verify final ground clearance",
                )
            ax, ay, az = self._xyz()
            err = ((ax-abs_x)**2 + (ay-abs_y)**2 + (az-effective_target_z)**2)**0.5
            return ActionResult(
                success=True,
                message=(
                    f"fly_to_ned OK: err={err:.3f}m, AGL={final_clearance:.2f}m"
                    + ("; unsafe target Z was raised" if adjusted else "")
                ),
                data={
                    "requested_target": [abs_x, abs_y, requested_z],
                    "effective_target": [abs_x, abs_y, az],
                    "ground_clearance": round(final_clearance, 3),
                    "target_adjusted": adjusted,
                },
            )
        except Exception as e:
            return ActionResult(success=False, message=str(e))
        finally:
            with self._manual_lock:
                self._manual_states.pop(control_vehicle, None)
            if self._autonomous_vehicle == control_vehicle:
                self._autonomous_vehicle = None

    def fly_to(self, position: Position, speed: float = 5.0) -> ActionResult:
        return self.fly_to_ned(position.north, position.east, position.down, speed)

    def fly_formation_segment(
        self,
        north: float,
        east: float,
        down: float,
        speed: float = 5.0,
    ) -> ActionResult:
        """Move one formation segment without repeating the cruise-height cycle."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        control_vehicle = self._vehicle_name
        self._autonomous_vehicle = control_vehicle
        with self._manual_lock:
            self._manual_states.pop(control_vehicle, None)
        try:
            if not self._hold_running:
                x, y, z = self._read_global_pose(self._vehicle_name)
                self._set_pose(x, y, z)
            clearance = self.get_ground_clearance(self._vehicle_name)
            if clearance is None:
                return ActionResult(
                    success=False,
                    message="Bottom distance sensor unavailable",
                )
            _, _, current_z = self._xyz()
            local_ground_z = current_z + clearance
            safe_z = min(float(down), local_ground_z - _MIN_FLIGHT_CLEARANCE_M)
            result = self._fly_smooth_raw(
                float(north),
                float(east),
                safe_z,
                speed=max(min(float(speed), 10.0), 1.0),
                step_interval=_FORMATION_STEP_INTERVAL_S,
                ground_check_interval=_FORMATION_GROUND_CHECK_STEPS,
                depth_check_interval=_FORMATION_DEPTH_CHECK_STEPS,
            )
            if result != "ok":
                return ActionResult(
                    success=False,
                    message=f"Formation segment stopped: {result}",
                )
            x, y, z = self._xyz()
            return ActionResult(
                success=True,
                message="Formation segment complete",
                data={"position": [x, y, z]},
            )
        except Exception as exc:
            return ActionResult(success=False, message=str(exc))
        finally:
            with self._manual_lock:
                self._manual_states.pop(control_vehicle, None)
            if self._autonomous_vehicle == control_vehicle:
                self._autonomous_vehicle = None

    def hover(self, duration: float = 5.0) -> ActionResult:
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        try:
            duration = min(max(float(duration), 0.0), 300.0)
            x, y, z = self._xyz()
            self._set_pose(x, y, z)
            time.sleep(duration)
            return ActionResult(
                success=True,
                message=f"Hovered {duration}s",
                data={
                    "position": [x, y, z],
                    "ground_clearance": self.get_ground_clearance(
                        self._vehicle_name,
                    ),
                },
            )
        except Exception as e:
            return ActionResult(success=False, message=str(e))

    def change_altitude(self, altitude: float, speed: float = 5.0) -> ActionResult:
        """Move vertically to a requested AGL at the current N/E position."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        try:
            target_clearance = min(
                max(float(altitude), _MIN_FLIGHT_CLEARANCE_M),
                120.0,
            )
            clearance = self._recover_and_ensure_clearance(
                _MIN_FLIGHT_CLEARANCE_M,
            )
            if clearance is None:
                return ActionResult(
                    success=False,
                    message="Bottom distance sensor unavailable",
                )
            x, y, z = self._xyz()
            target_z = z + clearance - target_clearance
            result = self._fly_smooth_raw(
                x,
                y,
                target_z,
                speed=max(float(speed), 1.0),
            )
            if result != "ok":
                return ActionResult(
                    success=False,
                    message=f"Altitude change stopped: {result}",
                )
            final_clearance = self.get_ground_clearance(self._vehicle_name)
            if final_clearance is None:
                return ActionResult(
                    success=False,
                    message="Final altitude could not be verified",
                )
            self._landed = False
            return ActionResult(
                success=True,
                message=f"Altitude changed to AGL={final_clearance:.2f}m",
                data={
                    "altitude": round(final_clearance, 3),
                    "requested_altitude": float(altitude),
                    "minimum_applied": target_clearance != float(altitude),
                    "position": list(self._xyz()),
                },
            )
        except Exception as exc:
            return ActionResult(success=False, message=str(exc))

    def change_altitude_relative(
        self,
        delta: float,
        speed: float = 5.0,
    ) -> ActionResult:
        clearance = self.get_ground_clearance(self._vehicle_name)
        if clearance is None:
            clearance = self._recover_and_ensure_clearance(
                _MIN_FLIGHT_CLEARANCE_M,
            )
        if clearance is None:
            return ActionResult(
                success=False,
                message="Bottom distance sensor unavailable",
            )
        return self.change_altitude(clearance + float(delta), speed=speed)

    def set_heading(self, heading_deg: float) -> ActionResult:
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        try:
            import math
            self._fly_yaw = math.radians(float(heading_deg) % 360.0)
            x, y, z = self._xyz()
            self._hold_x, self._hold_y, self._hold_z = x, y, z
            self._do_set_pose(x, y, z)
            return ActionResult(
                success=True,
                message=f"Heading set to {float(heading_deg) % 360.0:.1f} deg",
                data={"heading_deg": float(heading_deg) % 360.0},
            )
        except Exception as exc:
            return ActionResult(success=False, message=str(exc))

    def rotate_by(
        self,
        degrees: float,
        duration: float = 2.0,
    ) -> ActionResult:
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        try:
            import math
            start_yaw = float(getattr(self, "_fly_yaw", 0.0))
            delta = math.radians(float(degrees))
            duration = min(max(float(duration), 0.1), 60.0)
            steps = max(1, int(duration / 0.05))
            x, y, z = self._xyz()
            for step in range(1, steps + 1):
                self._fly_yaw = start_yaw + delta * step / steps
                self._hold_x, self._hold_y, self._hold_z = x, y, z
                self._do_set_pose(x, y, z)
                time.sleep(duration / steps)
            heading = math.degrees(self._fly_yaw) % 360.0
            return ActionResult(
                success=True,
                message=f"Rotated {float(degrees):.1f} deg",
                data={"heading_deg": heading},
            )
        except Exception as exc:
            return ActionResult(success=False, message=str(exc))

    def set_velocity_body(self, forward: float, right: float, down: float, yaw_rate: float = 0) -> ActionResult:
        return self.set_velocity_body_for(
            self._active_robot,
            forward,
            right,
            down,
            yaw_rate=yaw_rate,
        )

    def stop_velocity(self) -> ActionResult:
        """停止速度控制，保持当前位置。"""
        # hold 线程会自动维持当前位置，不需要额外操作
        return ActionResult(success=True, message='velocity stopped')

    def set_velocity_body_for(
        self,
        robot_id: str,
        forward: float,
        right: float,
        down: float,
        yaw_rate: float = 0,
    ) -> ActionResult:
        """Apply one cockpit velocity step to an explicitly selected UAV."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")

        vehicle_name = self.vehicle_for_robot(robot_id)
        if vehicle_name not in self._vehicle_names:
            return ActionResult(success=False, message=f"{robot_id} is not present in AirSim")

        try:
            if self._autonomous_vehicle == vehicle_name:
                logger.info(
                    "Cockpit manual takeover requested for %s/%s",
                    robot_id,
                    vehicle_name,
                )
                self.request_stop()
                deadline = time.monotonic() + 0.6
                while (
                    self._autonomous_vehicle == vehicle_name
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                with self._manual_lock:
                    self._manual_states.pop(vehicle_name, None)

            with self._manual_lock:
                import math

                now = time.monotonic()
                manual = self._manual_states.get(vehicle_name)
                if manual is None or now - manual["last_seen"] > 1.0:
                    x, y, z = self._read_global_pose(vehicle_name)
                    manual = {
                        "x": x,
                        "y": y,
                        "z": z,
                        "yaw": (
                            float(getattr(self, "_fly_yaw", 0.0))
                            if vehicle_name == self._vehicle_name
                            else 0.0
                        ),
                        "last_seen": now - 0.1,
                    }

                dt = min(max(now - manual["last_seen"], 0.05), 0.25)
                yaw = float(manual["yaw"])
                forward = float(forward)
                right = float(right)
                down = float(down)
                clearance = self.get_ground_clearance(vehicle_name)
                moving_horizontally = abs(forward) > 1e-6 or abs(right) > 1e-6
                if clearance is None and (moving_horizontally or down >= 0):
                    return ActionResult(
                        success=False,
                        message=(
                            f"{robot_id}/{vehicle_name} bottom range unavailable; "
                            "only upward recovery is allowed"
                        ),
                    )
                manual["x"] += (forward * math.cos(yaw) - right * math.sin(yaw)) * dt
                manual["y"] += (forward * math.sin(yaw) + right * math.cos(yaw)) * dt
                down_step = down * dt
                if down_step > 0 and clearance is not None:
                    down_step = min(
                        down_step,
                        max(clearance - _MIN_FLIGHT_CLEARANCE_M, 0.0),
                    )
                manual["z"] += down_step
                manual["yaw"] += math.radians(float(yaw_rate)) * dt
                manual["last_seen"] = now

                self._set_vehicle_global_pose(
                    vehicle_name,
                    manual["x"],
                    manual["y"],
                    manual["z"],
                    manual["yaw"],
                )
                new_clearance = self.get_ground_clearance(vehicle_name)
                if (
                    new_clearance is not None
                    and new_clearance < _MIN_FLIGHT_CLEARANCE_M
                ):
                    manual["z"] -= (
                        _MIN_FLIGHT_CLEARANCE_M - new_clearance + 0.3
                    )
                    self._set_vehicle_global_pose(
                        vehicle_name,
                        manual["x"],
                        manual["y"],
                        manual["z"],
                        manual["yaw"],
                    )
                self._manual_states[vehicle_name] = manual
                if vehicle_name == self._vehicle_name:
                    self._hold_x = manual["x"]
                    self._hold_y = manual["y"]
                    self._hold_z = manual["z"]
                    self._fly_yaw = manual["yaw"]

            return ActionResult(
                success=True,
                message=(
                    f"{robot_id}/{vehicle_name} "
                    f"position=({manual['x']:.2f},{manual['y']:.2f},{manual['z']:.2f})"
                ),
            )
        except Exception as exc:
            logger.warning("Cockpit control failed for %s/%s: %s", robot_id, vehicle_name, exc)
            return ActionResult(success=False, message=str(exc))

    def stop_velocity_for(self, robot_id: str) -> ActionResult:
        """Stop an explicitly selected UAV and hold its current altitude."""
        vehicle_name = self.vehicle_for_robot(robot_id)
        if vehicle_name not in self._vehicle_names:
            return ActionResult(success=False, message=f"{robot_id} is not present in AirSim")
        with self._manual_lock:
            manual = self._manual_states.get(vehicle_name)
            if manual:
                manual["last_seen"] = time.monotonic()
        return ActionResult(success=True, message=f"{robot_id}/{vehicle_name} stopped")

    def return_to_launch(self) -> ActionResult:
        """Return to this vehicle's activation point, then land by bottom range."""
        if not self._connected:
            return ActionResult(success=False, message="Not connected")
        try:
            home = self._vehicle_home_positions.get(
                self._vehicle_name,
                self._home_position,
            )
            if home is None:
                home = Position(
                    north=self._spawn_x,
                    east=self._spawn_y,
                    down=self._spawn_z,
                )

            clearance = self._recover_and_ensure_clearance(
                _CRUISE_CLEARANCE_M,
            )
            if clearance is None:
                return ActionResult(
                    success=False,
                    message="RTL aborted: bottom distance sensor unavailable",
                )

            _, _, z = self._xyz()
            flight = self.fly_to_ned(
                home.north,
                home.east,
                z,
                speed=8.0,
            )
            if not flight.success:
                return ActionResult(
                    success=False,
                    message=f"RTL flight failed: {flight.message}",
                    data=flight.data,
                )

            landing = self.land()
            return ActionResult(
                success=landing.success,
                message=f"RTL: {landing.message}",
                data={
                    "home": [home.north, home.east],
                    "position": list(self._xyz()),
                    **landing.data,
                },
            )
        except Exception as e:
            return ActionResult(success=False, message=str(e))
