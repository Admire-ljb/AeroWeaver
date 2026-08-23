"""In-memory multi-UAV simulator with deterministic point-mass dynamics."""

from __future__ import annotations

import logging
import math
import os
import threading
import time

from adapters.mock_dynamics import PointMassDynamics
from adapters.sim_adapter import ActionResult, GPSPosition, Position, SimAdapter, VehicleState


logger = logging.getLogger(__name__)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def _speed(values) -> float:
    items = [float(value) for value in list(values)[:3]]
    return math.sqrt(sum(value * value for value in items))


class MockAdapter(SimAdapter):
    """A lightweight 3D simulator for UI, agent and CI workflows.

    Normal flight commands submit targets to a background physics loop.  The
    explicit ``set_robot_position`` method remains available for scenario reset
    and scripted mission playback, where teleporting is intentional.
    """

    name = "mock"
    description = "Mock adapter (MPE-style 3D point-mass dynamics)"
    supported_vehicles = ["multirotor", "fixedwing", "rover"]

    def __init__(self, *, realtime_factor=None, dynamics=None):
        self._state_lock = threading.RLock()
        self._physics_condition = threading.Condition(self._state_lock)
        self._physics_stop = threading.Event()
        self._physics_thread = None
        self._dynamics = dynamics or PointMassDynamics()
        configured_factor = (
            realtime_factor
            if realtime_factor is not None
            else os.getenv("AEROWEAVER_MOCK_REALTIME_FACTOR", "2.0")
        )
        try:
            configured_factor = float(configured_factor)
        except (TypeError, ValueError):
            configured_factor = 2.0
        self._realtime_factor = _clamp(configured_factor, 0.1, 20.0)
        self._connected = False
        self._active_robot = "UAV_1"
        self._robot_states = {self._active_robot: self._default_robot_state()}
        self._operating_bounds = None
        self._bounded_robot_ids = set()
        self._position = Position(0.0, 0.0, 0.0)
        self._velocity_body = [0.0, 0.0, 0.0, 0.0]
        self._armed = False
        self._in_air = False
        self._battery = (12.6, 1.0)
        self._sync_active_cache_locked()

    def _default_robot_state(self) -> dict:
        return {
            "armed": False,
            "in_air": False,
            "position": [0.0, 0.0, 0.0],
            "velocity": [0.0, 0.0, 0.0],
            "command_velocity": [0.0, 0.0, 0.0],
            "yaw_rate": 0.0,
            "heading_deg": 0.0,
            "battery": (12.6, 1.0),
            "moving": False,
            "command_mode": "hold",
            "target": None,
            "max_speed": 5.0,
            "motion_seq": 0,
            "physics_steps": 0,
        }

    def _state_for_locked(self, robot_id: str) -> dict:
        return self._robot_states.setdefault(str(robot_id), self._default_robot_state())

    def _sync_active_cache_locked(self):
        state = self._state_for_locked(self._active_robot)
        self._position = Position(*state["position"])
        self._velocity_body = list(state["velocity"]) + [float(state["yaw_rate"])]
        self._armed = bool(state["armed"])
        self._in_air = bool(state["in_air"])
        self._battery = tuple(state["battery"])

    def _constrain_position_locked(self, robot_id: str, position) -> tuple[list[float], list[bool]]:
        values = [float(value) for value in list(position)[:3]]
        values = (values + [0.0, 0.0, 0.0])[:3]
        bounds = self._operating_bounds
        if not bounds or (self._bounded_robot_ids and str(robot_id) not in self._bounded_robot_ids):
            return values, [False, False]
        original = list(values)
        values[0] = _clamp(values[0], bounds["north_min"], bounds["north_max"])
        values[1] = _clamp(values[1], bounds["east_min"], bounds["east_max"])
        return values, [values[0] != original[0], values[1] != original[1]]

    def _guard_velocity_locked(self, robot_id: str, command) -> tuple[list[float], dict]:
        """Block outward N/E velocity near the active hard mission boundary."""
        values = [float(value) for value in list(command)[:3]]
        values = (values + [0.0, 0.0, 0.0])[:3]
        bounds = self._operating_bounds
        if not bounds or (self._bounded_robot_ids and str(robot_id) not in self._bounded_robot_ids):
            return values, {}
        state = self._state_for_locked(str(robot_id))
        position = list(state["position"])
        spans = (
            bounds["north_max"] - bounds["north_min"],
            bounds["east_max"] - bounds["east_min"],
        )
        margin = min(10.0, max(3.0, min(spans) * 0.12))
        distances = {
            "north_min": position[0] - bounds["north_min"],
            "north_max": bounds["north_max"] - position[0],
            "east_min": position[1] - bounds["east_min"],
            "east_max": bounds["east_max"] - position[1],
        }
        outside = [edge for edge, distance in distances.items() if distance < 0.0]
        near = [edge for edge, distance in distances.items() if 0.0 <= distance <= margin]
        adjusted = list(values)
        blocked_axes = []
        if position[0] <= bounds["north_min"] + margin and adjusted[0] < 0.0:
            adjusted[0] = 0.0
            blocked_axes.append("north_min")
        elif position[0] >= bounds["north_max"] - margin and adjusted[0] > 0.0:
            adjusted[0] = 0.0
            blocked_axes.append("north_max")
        if position[1] <= bounds["east_min"] + margin and adjusted[1] < 0.0:
            adjusted[1] = 0.0
            blocked_axes.append("east_min")
        elif position[1] >= bounds["east_max"] - margin and adjusted[1] > 0.0:
            adjusted[1] = 0.0
            blocked_axes.append("east_max")
        status = "outside" if outside else "warning" if near else "safe"
        nearest_edge = min(distances, key=distances.get)
        nearest_distance = round(float(distances[nearest_edge]), 3)
        warning = ""
        if outside:
            warning = (
                f"OUTSIDE hard mission boundary at {', '.join(outside)}; "
                "outward velocity blocked."
            )
        elif near:
            warning = (
                f"Boundary warning: {nearest_edge} is only "
                f"{max(0.0, nearest_distance):.1f} m away."
            )
        if blocked_axes:
            warning = f"{warning} Outward velocity blocked on {', '.join(blocked_axes)}.".strip()
        return adjusted, {
            "boundary_status": status,
            "boundary_warning": bool(warning),
            "boundary_message": warning,
            "boundary_nearest_edge": nearest_edge,
            "boundary_distance_m": nearest_distance,
            "boundary_blocked_axes": blocked_axes,
            "boundary_requested_velocity": values,
        }


    def _ensure_physics_thread(self):
        with self._state_lock:
            if self._physics_thread and self._physics_thread.is_alive():
                return
            self._physics_stop.clear()
            self._physics_thread = threading.Thread(
                target=self._physics_loop,
                daemon=True,
                name="mock-physics",
            )
            self._physics_thread.start()

    def _physics_loop(self):
        wall_interval = self._dynamics.dt / self._realtime_factor
        while not self._physics_stop.is_set():
            started = time.monotonic()
            with self._physics_condition:
                self._step_world_locked()
                self._physics_condition.notify_all()
            elapsed = time.monotonic() - started
            self._physics_stop.wait(max(0.001, wall_interval - elapsed))

    def _step_world_locked(self):
        for robot_id, state in self._robot_states.items():
            mode = state["command_mode"]
            if mode == "scripted":
                continue

            position = tuple(state["position"])
            velocity = tuple(state["velocity"])
            arrived = False
            if mode == "target" and state["target"] is not None:
                step = self._dynamics.target_step(
                    position,
                    velocity,
                    state["target"],
                    state["max_speed"],
                )
                arrived = step.arrived
            else:
                desired = (
                    state["command_velocity"]
                    if mode == "velocity"
                    else [0.0, 0.0, 0.0]
                )
                speed_limit = max(state["max_speed"], _speed(desired), 0.1)
                step = self._dynamics.velocity_step(
                    position,
                    velocity,
                    desired,
                    speed_limit,
                )

            next_position = list(step.position)
            next_velocity = list(step.velocity)
            next_position, boundary_hits = self._constrain_position_locked(robot_id, next_position)
            for axis, hit in enumerate(boundary_hits):
                if not hit:
                    continue
                next_velocity[axis] = 0.0
                command = float(state["command_velocity"][axis])
                at_minimum = next_position[axis] <= self._operating_bounds[
                    "north_min" if axis == 0 else "east_min"
                ]
                if (at_minimum and command < 0.0) or (not at_minimum and command > 0.0):
                    state["command_velocity"][axis] = 0.0
            if next_position[2] > 0.0:
                next_position[2] = 0.0
                next_velocity[2] = min(0.0, next_velocity[2])

            state["position"] = next_position
            state["velocity"] = next_velocity
            state["heading_deg"] = (
                float(state["heading_deg"])
                + float(state["yaw_rate"]) * self._dynamics.dt
            ) % 360.0
            state["physics_steps"] += 1

            if arrived:
                state["target"] = None
                state["command_velocity"] = [0.0, 0.0, 0.0]
                state["velocity"] = [0.0, 0.0, 0.0]
                state["command_mode"] = "hold"
                state["moving"] = False
            elif mode == "velocity":
                state["moving"] = (
                    _speed(state["command_velocity"]) > 0.01
                    or abs(float(state["yaw_rate"])) > 0.01
                    or _speed(state["velocity"]) > 0.03
                )
            elif mode in {"hold", "brake"}:
                if _speed(state["velocity"]) <= 0.03:
                    state["velocity"] = [0.0, 0.0, 0.0]
                    state["command_mode"] = "hold"
                    state["moving"] = False
                else:
                    state["moving"] = True
            else:
                state["moving"] = True

            if state["position"][2] >= -0.02 and mode == "velocity":
                if float(state["command_velocity"][2]) > 0.0:
                    state["in_air"] = False

            if robot_id == self._active_robot:
                self._sync_active_cache_locked()

    def _interrupt_locked(self, state: dict, *, brake=True):
        state["motion_seq"] += 1
        state["target"] = None
        state["command_velocity"] = [0.0, 0.0, 0.0]
        state["yaw_rate"] = 0.0
        state["command_mode"] = "brake" if brake else "hold"
        if not brake:
            state["velocity"] = [0.0, 0.0, 0.0]
            state["moving"] = False

    def _execute_target(self, robot_id: str, target, speed: float, label: str) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        speed = max(0.1, float(speed))
        target = [float(value) for value in list(target)[:3]]
        if len(target) != 3:
            return ActionResult(False, "Target must contain [north, east, down]")

        with self._physics_condition:
            state = self._state_for_locked(robot_id)
            target, _ = self._constrain_position_locked(robot_id, target)
            start = list(state["position"])
            distance = math.dist(start, target)
            state["motion_seq"] += 1
            motion_seq = state["motion_seq"]
            start_step = int(state["physics_steps"])
            state["target"] = target
            state["command_velocity"] = [0.0, 0.0, 0.0]
            state["yaw_rate"] = 0.0
            state["max_speed"] = speed
            state["command_mode"] = "target"
            state["moving"] = True
            if target[2] < -0.02:
                state["armed"] = True
                state["in_air"] = True
            self._physics_condition.notify_all()

        simulated_timeout = max(4.0, distance / speed * 4.0 + 4.0)
        wall_timeout = simulated_timeout / self._realtime_factor + 1.0
        started = time.monotonic()
        deadline = started + wall_timeout

        with self._physics_condition:
            while True:
                state = self._state_for_locked(robot_id)
                if state["motion_seq"] != motion_seq:
                    return ActionResult(
                        False,
                        f"{label} interrupted by a newer command (mock)",
                        {"position": list(state["position"]), "interrupted": True},
                        round((time.monotonic() - started) * self._realtime_factor, 2),
                    )
                if state["target"] is None and state["command_mode"] == "hold":
                    final = list(state["position"])
                    simulated_duration = (time.monotonic() - started) * self._realtime_factor
                    return ActionResult(
                        True,
                        f"{label} complete (mock dynamics)",
                        {
                            "position": final,
                            "velocity": list(state["velocity"]),
                            "physics_steps": int(state["physics_steps"]) - start_step,
                            "realtime_factor": self._realtime_factor,
                        },
                        round(simulated_duration, 2),
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    self._interrupt_locked(state, brake=False)
                    if robot_id == self._active_robot:
                        self._sync_active_cache_locked()
                    return ActionResult(
                        False,
                        f"{label} timed out (mock dynamics)",
                        {"position": list(state["position"]), "target": target},
                        round(simulated_timeout, 2),
                    )
                self._physics_condition.wait(timeout=min(0.25, remaining))

    def set_active_robot(self, robot_id: str):
        with self._state_lock:
            self._active_robot = str(robot_id or "UAV_1")
            self._state_for_locked(self._active_robot)
            self._sync_active_cache_locked()

    def get_active_robot(self) -> str:
        with self._state_lock:
            return self._active_robot

    @property
    def realtime_factor(self) -> float:
        return self._realtime_factor

    def seed_fleet(self, robots: dict):
        """Initialize the complete mock fleet from WorldModel robot data."""
        with self._physics_condition:
            for robot_id, robot in (robots or {}).items():
                position = list(robot.get("position") or [0.0, 0.0, 0.0])
                position = (position + [0.0, 0.0, 0.0])[:3]
                battery = float(robot.get("battery", 100.0))
                if battery > 1.0:
                    battery /= 100.0
                state = self._default_robot_state()
                state.update({
                    "armed": bool(robot.get("armed", False)),
                    "in_air": bool(robot.get("in_air", False)),
                    "position": [float(value) for value in position],
                    "battery": (12.6, _clamp(battery, 0.0, 1.0)),
                })
                self._robot_states[str(robot_id)] = state
            self._state_for_locked(self._active_robot)
            self._sync_active_cache_locked()
            self._physics_condition.notify_all()

    def retain_fleet(self, robot_ids):
        """Remove inactive robots while keeping at least one active robot."""
        allowed = {str(robot_id) for robot_id in robot_ids if str(robot_id)}
        if not allowed:
            raise ValueError("Mock fleet must contain at least one robot")
        with self._physics_condition:
            self._robot_states = {
                robot_id: state
                for robot_id, state in self._robot_states.items()
                if robot_id in allowed
            }
            for robot_id in allowed:
                self._state_for_locked(robot_id)
            if self._active_robot not in self._robot_states:
                self._active_robot = sorted(self._robot_states)[0]
            self._sync_active_cache_locked()
            self._physics_condition.notify_all()

    def get_robot_snapshot(self) -> dict:
        """Return a thread-safe fleet telemetry snapshot."""
        with self._state_lock:
            return {
                robot_id: {
                    "position": list(state["position"]),
                    "velocity": list(state["velocity"]),
                    "command_velocity": list(state["command_velocity"]),
                    "heading_deg": float(state["heading_deg"]),
                    "battery": float(state["battery"][1]),
                    "armed": bool(state["armed"]),
                    "in_air": bool(state["in_air"]),
                    "moving": bool(state["moving"]),
                    "motion_mode": state["command_mode"],
                    "target": list(state["target"]) if state["target"] is not None else None,
                }
                for robot_id, state in self._robot_states.items()
            }

    def set_operating_bounds(self, bounds: dict, robot_ids=None) -> dict:
        """Install a hard rectangular N/E boundary for the active mission."""
        required = ("north_min", "north_max", "east_min", "east_max")
        normalized = {key: float(bounds[key]) for key in required}
        if (
            normalized["north_max"] <= normalized["north_min"]
            or normalized["east_max"] <= normalized["east_min"]
        ):
            raise ValueError("Operating bounds must have positive north/east spans")
        with self._physics_condition:
            self._operating_bounds = normalized
            self._bounded_robot_ids = {str(item) for item in (robot_ids or []) if str(item)}
            for robot_id, state in self._robot_states.items():
                if self._bounded_robot_ids and robot_id not in self._bounded_robot_ids:
                    continue
                state["position"], _ = self._constrain_position_locked(robot_id, state["position"])
                if state["target"] is not None:
                    state["target"], _ = self._constrain_position_locked(robot_id, state["target"])
            self._sync_active_cache_locked()
            self._physics_condition.notify_all()
        return dict(normalized)

    def clear_operating_bounds(self) -> None:
        with self._physics_condition:
            self._operating_bounds = None
            self._bounded_robot_ids.clear()
            self._physics_condition.notify_all()

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
        """Reset or externally script one UAV pose without selecting it."""
        with self._physics_condition:
            state = self._state_for_locked(str(robot_id))
            state["motion_seq"] += 1
            bounded_position, _ = self._constrain_position_locked(
                str(robot_id), [float(north), float(east), min(0.0, float(down))]
            )
            state["position"] = bounded_position
            if velocity is not None:
                values = [float(value) for value in list(velocity)[:3]]
                state["velocity"] = (values + [0.0, 0.0, 0.0])[:3]
            else:
                state["velocity"] = [0.0, 0.0, 0.0]
            state["command_velocity"] = [0.0, 0.0, 0.0]
            state["target"] = None
            state["moving"] = bool(moving)
            state["command_mode"] = "scripted" if moving else "hold"
            state["in_air"] = bool(in_air)
            state["armed"] = bool(in_air) or bool(state["armed"])
            if str(robot_id) == self._active_robot:
                self._sync_active_cache_locked()
            self._physics_condition.notify_all()

    def reset_robot_pose(self, robot_id: str, position: list, *, in_air: bool = True) -> ActionResult:
        """Expose direct pose assignment only to the guarded initialization skill."""
        values = list(position or [])
        if len(values) < 3:
            return ActionResult(False, "position requires three coordinates")
        self.set_robot_position(
            str(robot_id),
            float(values[0]),
            float(values[1]),
            float(values[2]),
            moving=False,
            in_air=bool(in_air),
        )
        final_position = self.get_robot_snapshot()[str(robot_id)]["position"]
        return ActionResult(True, "scene start pose initialized", {"position": final_position})

    def connect(self, connection_str="mock://", timeout=1.0) -> bool:
        self._connected = True
        self._ensure_physics_thread()
        logger.info(
            "MockAdapter connected: dt=%.3fs realtime_factor=%.2fx",
            self._dynamics.dt,
            self._realtime_factor,
        )
        return True

    def disconnect(self):
        self._connected = False
        self._physics_stop.set()
        thread = self._physics_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def is_connected(self):
        return self._connected

    def get_state(self) -> VehicleState:
        with self._state_lock:
            state = self._state_for_locked(self._active_robot)
            position = Position(*state["position"])
            return VehicleState(
                armed=bool(state["armed"]),
                in_air=bool(state["in_air"]),
                mode="MOCK_KINEMATIC",
                position_ned=position,
                position_gps=GPSPosition(47.397971, 8.546163, position.altitude),
                battery_voltage=float(state["battery"][0]),
                battery_percent=float(state["battery"][1]),
                velocity=list(state["velocity"]),
            )

    def get_position(self) -> Position:
        with self._state_lock:
            state = self._state_for_locked(self._active_robot)
            return Position(*state["position"])

    def get_gps(self) -> GPSPosition:
        position = self.get_position()
        return GPSPosition(47.397971, 8.546163, position.altitude)

    def get_battery(self) -> tuple:
        with self._state_lock:
            return tuple(self._state_for_locked(self._active_robot)["battery"])

    def is_armed(self) -> bool:
        with self._state_lock:
            return bool(self._state_for_locked(self._active_robot)["armed"])

    def is_in_air(self) -> bool:
        with self._state_lock:
            return bool(self._state_for_locked(self._active_robot)["in_air"])

    def arm(self) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        with self._state_lock:
            state = self._state_for_locked(self._active_robot)
            state["armed"] = True
            self._sync_active_cache_locked()
        return ActionResult(True, "ARM (mock dynamics)")

    def disarm(self) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        with self._physics_condition:
            state = self._state_for_locked(self._active_robot)
            self._interrupt_locked(state, brake=False)
            state["armed"] = False
            self._sync_active_cache_locked()
            self._physics_condition.notify_all()
        return ActionResult(True, "DISARM (mock dynamics)")

    def takeoff(self, altitude=5.0) -> ActionResult:
        altitude = max(0.5, abs(float(altitude)))
        robot_id = self.get_active_robot()
        with self._state_lock:
            state = self._state_for_locked(robot_id)
            target = [state["position"][0], state["position"][1], -altitude]
            state["armed"] = True
            state["in_air"] = True
        result = self._execute_target(robot_id, target, min(5.0, max(2.0, altitude)), "Takeoff")
        if result.success:
            result.data["altitude"] = altitude
        return result

    def land(self) -> ActionResult:
        robot_id = self.get_active_robot()
        with self._state_lock:
            state = self._state_for_locked(robot_id)
            target = [state["position"][0], state["position"][1], 0.0]
        result = self._execute_target(robot_id, target, 2.5, "Landing")
        if result.success:
            with self._state_lock:
                state = self._state_for_locked(robot_id)
                state["in_air"] = False
                state["armed"] = False
                self._sync_active_cache_locked()
        return result

    def fly_to_ned(self, north, east, down, speed=2.0) -> ActionResult:
        robot_id = self.get_active_robot()
        target = [float(north), float(east), min(0.0, float(down))]
        return self._execute_target(robot_id, target, speed, "Fly-to")

    def hover(self, duration=5.0) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        robot_id = self.get_active_robot()
        self.stop_velocity_for(robot_id)
        requested = max(0.0, float(duration))
        time.sleep(min(requested / self._realtime_factor, 0.5))
        return ActionResult(
            True,
            f"Hover {requested:.1f}s (mock dynamics)",
            {"position": self.get_position().to_list()},
            requested,
        )

    def set_velocity_body(self, forward, right, down, yaw_rate=0.0) -> ActionResult:
        return self.set_velocity_body_for(
            self.get_active_robot(), forward, right, down, yaw_rate=yaw_rate
        )

    def set_velocity_body_for(self, robot_id, forward, right, down, yaw_rate=0.0) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        command = [float(forward), float(right), float(down)]
        with self._physics_condition:
            state = self._state_for_locked(str(robot_id))
            state["motion_seq"] += 1
            state["target"] = None
            state["command_velocity"] = command
            state["yaw_rate"] = float(yaw_rate)
            state["max_speed"] = max(0.1, _speed(command))
            state["command_mode"] = "velocity"
            state["moving"] = _speed(command) > 0.01 or abs(float(yaw_rate)) > 0.01
            if state["moving"]:
                state["armed"] = True
                state["in_air"] = True
            position = list(state["position"])
            actual_velocity = list(state["velocity"])
            if str(robot_id) == self._active_robot:
                self._sync_active_cache_locked()
            self._physics_condition.notify_all()
        return ActionResult(
            True,
            "velocity target accepted (mock dynamics)",
            {
                "velocity_body": command + [float(yaw_rate)],
                "velocity_ned": actual_velocity,
                "position": position,
            },
            self._dynamics.dt,
        )

    def set_velocity_ned_for(self, robot_id, north, east, down) -> ActionResult:
        """Set a persistent world-frame velocity for autonomous Mock agents."""
        if not self._connected:
            return ActionResult(False, "Not connected")
        command = [float(north), float(east), float(down)]
        with self._physics_condition:
            command, boundary_info = self._guard_velocity_locked(str(robot_id), command)
            state = self._state_for_locked(str(robot_id))
            state["motion_seq"] += 1
            state["target"] = None
            state["command_velocity"] = command
            state["yaw_rate"] = 0.0
            state["max_speed"] = max(0.1, _speed(command))
            state["command_mode"] = "velocity"
            state["moving"] = _speed(command) > 0.01
            if state["moving"]:
                state["armed"] = True
                state["in_air"] = True
            position = list(state["position"])
            if str(robot_id) == self._active_robot:
                self._sync_active_cache_locked()
            self._physics_condition.notify_all()
        return ActionResult(
            True,
            "persistent NED velocity accepted (mock dynamics)",
            {
                "velocity_ned": command,
                "position": position,
                "persistent": True,
                **boundary_info,
            },
            self._dynamics.dt,
        )

    def stop_velocity(self) -> ActionResult:
        return self.stop_velocity_for(self.get_active_robot())

    def stop_velocity_for(self, robot_id: str) -> ActionResult:
        if not self._connected:
            return ActionResult(False, "Not connected")
        with self._physics_condition:
            state = self._state_for_locked(str(robot_id))
            self._interrupt_locked(state, brake=True)
            position = list(state["position"])
            if str(robot_id) == self._active_robot:
                self._sync_active_cache_locked()
            self._physics_condition.notify_all()
        return ActionResult(
            True,
            "velocity braking requested (mock dynamics)",
            {"position": position},
        )

    def return_to_launch(self) -> ActionResult:
        robot_id = self.get_active_robot()
        result = self._execute_target(robot_id, [0.0, 0.0, 0.0], 5.0, "RTL")
        if result.success:
            with self._state_lock:
                state = self._state_for_locked(robot_id)
                state["in_air"] = False
                state["armed"] = False
                self._sync_active_cache_locked()
        return result
