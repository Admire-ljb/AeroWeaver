"""
mock_adapter.py
Mock 仿真适配器 —— 纯内存模拟，不依赖任何外部仿真环境。
用于单元测试、离线开发、CI/CD。
"""

import time
import logging
import threading
from adapters.sim_adapter import SimAdapter, Position, GPSPosition, VehicleState, ActionResult

logger = logging.getLogger(__name__)


class MockAdapter(SimAdapter):
    """Mock 仿真适配器，所有操作纯内存模拟。"""

    name = "mock"
    description = "Mock adapter (in-memory simulation, no external dependencies)"
    supported_vehicles = ["multirotor", "fixedwing", "rover"]

    def __init__(self):
        self._state_lock = threading.RLock()
        self._connected = False
        self._armed = False
        self._in_air = False
        self._position = Position(0, 0, 0)
        self._velocity_body = [0.0, 0.0, 0.0, 0.0]
        self._battery = (12.6, 1.0)
        self._active_robot = "UAV_1"
        self._robot_states = {}
        self._capture_active_state()

    def _default_robot_state(self) -> dict:
        return {
            "armed": False,
            "in_air": False,
            "position": Position(0, 0, 0),
            "velocity_body": [0.0, 0.0, 0.0, 0.0],
            "battery": (12.6, 1.0),
            "moving": False,
        }

    def _capture_active_state(self):
        with self._state_lock:
            previous = self._robot_states.get(self._active_robot, {})
            self._robot_states[self._active_robot] = {
                "armed": self._armed,
                "in_air": self._in_air,
                "position": Position(self._position.north, self._position.east, self._position.down),
                "velocity_body": list(self._velocity_body),
                "battery": tuple(self._battery),
                "moving": bool(previous.get("moving", False)),
            }

    def _restore_active_state(self):
        with self._state_lock:
            snapshot = self._robot_states.setdefault(self._active_robot, self._default_robot_state())
            pos = snapshot["position"]
            self._armed = bool(snapshot["armed"])
            self._in_air = bool(snapshot["in_air"])
            self._position = Position(pos.north, pos.east, pos.down)
            self._velocity_body = list(snapshot["velocity_body"])
            self._battery = tuple(snapshot["battery"])

    def set_active_robot(self, robot_id: str):
        with self._state_lock:
            robot_id = str(robot_id or "UAV_1")
            if robot_id == self._active_robot:
                return
            self._capture_active_state()
            self._active_robot = robot_id
            self._restore_active_state()

    def get_active_robot(self) -> str:
        with self._state_lock:
            return self._active_robot

    def seed_fleet(self, robots: dict):
        """Initialize the complete mock fleet from WorldModel robot data."""
        with self._state_lock:
            for robot_id, robot in (robots or {}).items():
                position = list(robot.get("position") or [0.0, 0.0, 0.0])
                position = (position + [0.0, 0.0, 0.0])[:3]
                battery = float(robot.get("battery", 100.0))
                if battery > 1.0:
                    battery /= 100.0
                self._robot_states[str(robot_id)] = {
                    "armed": bool(robot.get("armed", False)),
                    "in_air": bool(robot.get("in_air", False)),
                    "position": Position(*[float(value) for value in position]),
                    "velocity_body": [0.0, 0.0, 0.0, 0.0],
                    "battery": (12.6, max(0.0, min(1.0, battery))),
                    "moving": False,
                }
            self._restore_active_state()

    def get_robot_snapshot(self) -> dict:
        """Return a thread-safe snapshot for fleet telemetry synchronization."""
        with self._state_lock:
            return {
                robot_id: {
                    "position": state["position"].to_list(),
                    "velocity": list(state["velocity_body"][:3]),
                    "battery": float(state["battery"][1]),
                    "armed": bool(state["armed"]),
                    "in_air": bool(state["in_air"]),
                    "moving": bool(state.get("moving", False)),
                }
                for robot_id, state in self._robot_states.items()
            }

    def set_robot_position(
        self,
        robot_id: str,
        north: float,
        east: float,
        down: float,
        *,
        velocity=None,
        moving: bool = False,
        in_air: bool = True,
    ):
        """Set one simulated UAV pose without changing the selected UAV."""
        with self._state_lock:
            robot_id = str(robot_id)
            state = self._robot_states.setdefault(robot_id, self._default_robot_state())
            state["position"] = Position(float(north), float(east), float(down))
            if velocity is not None:
                values = [float(value) for value in list(velocity)[:3]]
                state["velocity_body"] = values + [0.0] * (4 - len(values))
            state["moving"] = bool(moving)
            state["in_air"] = bool(in_air)
            state["armed"] = bool(in_air) or bool(state["armed"])
            if robot_id == self._active_robot:
                self._restore_active_state()

    def connect(self, connection_str="mock://", timeout=1.0) -> bool:
        self._connected = True
        logger.info("MockAdapter: ✅ 已连接 (mock)")
        return True

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def get_state(self) -> VehicleState:
        with self._state_lock:
            return VehicleState(
                armed=self._armed, in_air=self._in_air, mode="MOCK",
                position_ned=self._position,
                position_gps=GPSPosition(47.397971, 8.546163, self._position.altitude),
                battery_voltage=self._battery[0], battery_percent=self._battery[1],
                velocity=self._velocity_body[:3],
            )

    def get_position(self) -> Position:
        return self._position

    def get_gps(self) -> GPSPosition:
        return GPSPosition(47.397971, 8.546163, self._position.altitude)

    def get_battery(self) -> tuple:
        return self._battery

    def is_armed(self) -> bool:
        return self._armed

    def is_in_air(self) -> bool:
        return self._in_air

    def arm(self) -> ActionResult:
        self._armed = True
        self._capture_active_state()
        return ActionResult(True, "ARM (mock)")

    def disarm(self) -> ActionResult:
        self._armed = False
        self._capture_active_state()
        return ActionResult(True, "DISARM (mock)")

    def takeoff(self, altitude=5.0) -> ActionResult:
        self._armed = True
        self._in_air = True
        self._position = Position(0, 0, -altitude)
        self._capture_active_state()
        time.sleep(0.1)
        return ActionResult(True, f"起飞到 {altitude}m (mock)", {"altitude": altitude}, 0.1)

    def land(self) -> ActionResult:
        self._in_air = False
        self._position = Position(self._position.north, self._position.east, 0)
        self._armed = False
        self._capture_active_state()
        time.sleep(0.1)
        return ActionResult(True, "降落 (mock)", duration=0.1)

    def fly_to_ned(self, north, east, down, speed=2.0) -> ActionResult:
        self._position = Position(north, east, down)
        self._capture_active_state()
        dist = (north**2 + east**2 + down**2) ** 0.5
        dur = dist / speed if speed > 0 else 0.1
        time.sleep(min(dur, 0.5))
        return ActionResult(True, f"到达 NED=({north},{east},{down}) (mock)",
                          {"position": [north, east, down]}, round(dur, 2))

    def hover(self, duration=5.0) -> ActionResult:
        time.sleep(min(duration, 0.5))
        return ActionResult(True, f"悬停 {duration}s (mock)",
                          {"position": self._position.to_list()}, duration)


    def set_velocity_body(self, forward: float, right: float, down: float, yaw_rate: float = 0.0) -> ActionResult:
        """Apply one small body-frame velocity step for keyboard/cockpit control.

        The mock adapter has no background physics loop, so each Socket.IO
        velocity event advances the in-memory position by a short fixed time
        step. This keeps the Web cockpit path usable without PX4/Gazebo/AirSim.
        """
        if not self._connected:
            return ActionResult(False, "Not connected")

        dt = 0.1
        self._velocity_body = [float(forward), float(right), float(down), float(yaw_rate)]
        self._position = Position(
            self._position.north + float(forward) * dt,
            self._position.east + float(right) * dt,
            self._position.down + float(down) * dt,
        )
        if any(abs(v) > 1e-9 for v in self._velocity_body[:3]):
            self._in_air = True
        self._capture_active_state()
        return ActionResult(
            True,
            "velocity_body sent (mock)",
            {"velocity_body": self._velocity_body, "position": self._position.to_list()},
            dt,
        )

    def stop_velocity(self) -> ActionResult:
        """Stop keyboard/cockpit velocity control in mock mode."""
        if not self._connected:
            return ActionResult(False, "Not connected")
        self._velocity_body = [0.0, 0.0, 0.0, 0.0]
        self._capture_active_state()
        return ActionResult(True, "velocity stopped (mock)", {"position": self._position.to_list()})

    def return_to_launch(self) -> ActionResult:
        self._position = Position(0, 0, 0)
        self._velocity_body = [0.0, 0.0, 0.0, 0.0]
        self._in_air = False
        self._armed = False
        self._capture_active_state()
        return ActionResult(True, "RTL (mock)", duration=0.1)
