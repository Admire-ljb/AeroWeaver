"""Mission-level fleet resource management skills."""

import time
from collections.abc import Callable

from skills.base_skill import Skill, SkillResult


_MAX_AUTONOMOUS_FLEET_SIZE = 10
_fleet_resize_handler: Callable | None = None


def configure_fleet_resize_handler(handler: Callable | None) -> None:
    """Inject the server-owned fleet resize operation."""
    global _fleet_resize_handler
    _fleet_resize_handler = handler


class SetFleetSize(Skill):
    """Let the agent select the active UAV count before mission actions."""

    name = "set_fleet_size"
    description = (
        "Adjust the number of active UAVs before a mission. Use only when the "
        "operator requests a count or mission coverage requires a different fleet size."
    )
    skill_type = "hard"
    skill_level = "advanced"
    robot_type = ["UAV"]
    preconditions = []
    input_schema = {
        "count": "integer active UAV count from 1 to 10",
        "reason": "short explanation of why this fleet size fits the mission",
    }
    output_schema = {
        "active_count": "number of UAVs active after resizing",
        "activated": "UAV IDs added to the active fleet",
        "deactivated": "UAV IDs removed from the active fleet",
        "adapter": "mock or AirSim fleet backend",
    }
    cost = 0.2

    def execute(self, input_data):
        started = time.time()
        try:
            count = int(input_data.get("count"))
        except (TypeError, ValueError):
            return SkillResult(
                success=False,
                error_msg="count must be an integer from 1 to 10",
                cost_time=round(time.time() - started, 4),
            )
        if not 1 <= count <= _MAX_AUTONOMOUS_FLEET_SIZE:
            return SkillResult(
                success=False,
                error_msg="count must be between 1 and 10",
                cost_time=round(time.time() - started, 4),
            )
        if _fleet_resize_handler is None:
            return SkillResult(
                success=False,
                error_msg="fleet resize handler is not configured",
                cost_time=round(time.time() - started, 4),
            )

        reason = str(input_data.get("reason") or "mission resource planning").strip()
        try:
            result = _fleet_resize_handler(
                count=count,
                reason=reason,
                robot_id=str(input_data.get("robot_id") or "UAV_1"),
            )
        except Exception as exc:
            return SkillResult(
                success=False,
                error_msg=f"fleet resize failed: {exc}",
                cost_time=round(time.time() - started, 4),
            )

        if not result.get("ok"):
            return SkillResult(
                success=False,
                output=result,
                error_msg=str(result.get("error") or "fleet resize failed"),
                cost_time=round(time.time() - started, 4),
            )

        active_count = int(result.get("active_count", count))
        output = {
            **result,
            "requested_count": count,
            "reason": reason,
            "completion_summary": (
                f"Fleet resized to {active_count} active UAVs for the mission."
            ),
            "completion_summary_zh": (
                f"机队规模已调整为 {active_count} 架任务无人机。"
            ),
        }
        return SkillResult(
            success=True,
            output=output,
            cost_time=round(time.time() - started, 4),
            logs=[f"Fleet resized to {active_count} UAVs: {reason}"],
        )
