"""System-only scene initialization skills."""

from __future__ import annotations

import math
import threading
from contextlib import contextmanager

from skills.base_skill import Skill, SkillResult


_scene_reset_context = threading.local()


@contextmanager
def scene_reset_window(mission_id: str):
    """Authorize initialization skills only for the current server-owned reset block."""
    previous = getattr(_scene_reset_context, "mission_id", None)
    _scene_reset_context.mission_id = str(mission_id)
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_scene_reset_context, "mission_id")
            except AttributeError:
                pass
        else:
            _scene_reset_context.mission_id = previous


class TeleportInitialize(Skill):
    """Set one UAV pose atomically during the pre-mission scene reset window."""

    name = "teleport_initialize"
    description = (
        "SYSTEM ONLY: teleport to a mission start pose. This skill is rejected outside "
        "the server-owned scene reset that runs before a global mission."
    )
    skill_type = "hard"
    skill_level = "basic"
    robot_type = ["UAV"]
    preconditions = []
    cost = 0.0
    input_schema = {
        "position": "[north, east, down] mission start position",
        "mission_id": "server-issued mission identifier",
        "in_air": "whether the initialized UAV starts airborne",
    }
    output_schema = {
        "position": "[north, east, down]",
        "mission_id": "authorized mission identifier",
        "initialization_only": "always true",
    }

    def check_precondition(self, robot_state: dict) -> bool:
        return True

    def execute(self, input_data: dict) -> SkillResult:
        authorized_mission = str(getattr(_scene_reset_context, "mission_id", "") or "")
        requested_mission = str(input_data.get("mission_id") or "")
        if not authorized_mission or requested_mission != authorized_mission:
            return SkillResult(
                success=False,
                error_msg=(
                    "teleport_initialize is restricted to the server-owned "
                    "pre-mission scene reset window"
                ),
                logs=["Initialization teleport rejected outside scene reset"],
            )

        position = list(input_data.get("position") or [])
        if len(position) < 3:
            return SkillResult(success=False, error_msg="position requires [north, east, down]")
        position = [float(value) for value in position[:3]]
        position[2] = min(-1.0, position[2])
        robot_id = str(input_data.get("robot_id") or "")
        in_air = bool(input_data.get("in_air", True))

        from adapters.adapter_manager import get_adapter

        adapter = get_adapter()
        reset_pose = getattr(adapter, "reset_robot_pose", None) if adapter is not None else None
        if not callable(reset_pose):
            return SkillResult(
                success=False,
                error_msg=f"{getattr(adapter, 'name', 'adapter')} does not support scene reset poses",
            )

        result = reset_pose(robot_id, position, in_air=in_air)
        return SkillResult(
            success=bool(result.success),
            output={
                "position": list((result.data or {}).get("position") or position),
                "mission_id": authorized_mission,
                "initialization_only": True,
            },
            error_msg="" if result.success else result.message,
            cost_time=float(getattr(result, "duration", 0.0) or 0.0),
            logs=[f"teleport_initialize {robot_id}: {result.message}"],
        )


class SteerVelocity(Skill):
    """Apply a persistent velocity chosen by one UAV agent in Mock missions."""

    name = "steer_velocity"
    description = (
        "Mock motion-agent skill: steer this UAV along a horizontal NED direction. "
        "The velocity remains active until this UAV chooses another direction or the mission ends."
    )
    skill_type = "hard"
    skill_level = "basic"
    robot_type = ["UAV"]
    preconditions = []
    cost = 0.1
    input_schema = {
        "direction": "normalized [north, east, down] direction",
        "speed_mps": "persistent speed in m/s",
    }
    output_schema = {
        "velocity_ned": "accepted [north, east, down] velocity",
        "persistent": "true until changed or stopped",
    }

    def check_precondition(self, robot_state: dict) -> bool:
        return True

    def execute(self, input_data: dict) -> SkillResult:
        direction = list(input_data.get("direction") or [])
        if len(direction) < 2:
            return SkillResult(success=False, error_msg="direction requires [north, east, down]")
        try:
            north = float(direction[0])
            east = float(direction[1])
            down = float(direction[2]) if len(direction) > 2 else 0.0
            # The mission planner may assign a faster evader. Mock dynamics
            # applies its own acceleration and velocity limits; this skill
            # should not silently erase a valid per-UAV speed profile.
            speed = min(max(float(input_data.get("speed_mps", 5.0)), 0.0), 30.0)
        except (TypeError, ValueError):
            return SkillResult(success=False, error_msg="direction and speed must be numeric")

        length = math.sqrt(north * north + east * east + down * down)
        if length <= 1e-9 or speed <= 1e-9:
            return SkillResult(success=False, error_msg="steer_velocity requires a non-zero motion vector")
        velocity = [north / length * speed, east / length * speed, down / length * speed]

        from adapters.adapter_manager import get_adapter

        adapter = get_adapter()
        robot_id = str(input_data.get("robot_id") or "")
        setter = getattr(adapter, "set_velocity_ned_for", None) if adapter is not None else None
        if not callable(setter):
            return SkillResult(
                success=False,
                error_msg=f"{getattr(adapter, 'name', 'adapter')} does not support persistent NED steering",
            )
        result = setter(robot_id, *velocity)
        return SkillResult(
            success=bool(result.success),
            output={
                "velocity_ned": list((result.data or {}).get("velocity_ned") or velocity),
                "requested_velocity_ned": [round(value, 4) for value in velocity],
                "persistent": bool(result.success),
                "position": list((result.data or {}).get("position") or []),
                **dict(result.data or {}),
            },
            error_msg="" if result.success else result.message,
            cost_time=float(getattr(result, "duration", 0.0) or 0.0),
            logs=[f"steer_velocity {robot_id}: {result.message}"],
        )
