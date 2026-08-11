"""Robot-scoped execution authority for one autonomous UAV agent."""

from __future__ import annotations

from runtime.exector import ExecutionResult


def _normalize_robot_id(robot_id: str) -> str:
    return str(robot_id or "").strip().upper().replace("-", "_")


class UAVAgentRuntime:
    """Expose skill dispatch while preventing an agent from controlling peers."""

    def __init__(self, runtime, robot_id: str):
        owner = _normalize_robot_id(robot_id)
        if not owner or owner == "COMMANDER":
            raise ValueError("A UAV agent runtime requires a physical UAV owner")
        self._runtime = runtime
        self.robot_id = owner

    def dispatch_skill(self, step: dict) -> ExecutionResult:
        requested = _normalize_robot_id((step or {}).get("robot") or self.robot_id)
        skill_name = str((step or {}).get("skill") or "")
        if requested != self.robot_id:
            return ExecutionResult(
                success=False,
                skill=skill_name,
                robot=requested,
                error_msg=(
                    f"Agent {self.robot_id} cannot control {requested}; "
                    "each UAV may dispatch skills only to its own body"
                ),
                logs=[f"[AgentAuthority] rejected {self.robot_id} -> {requested}"],
            )

        owned_step = dict(step or {})
        owned_step["robot"] = self.robot_id
        owned_step["parameters"] = dict(owned_step.get("parameters") or {})
        return self._runtime.dispatch_skill(owned_step)
