"""Body-local task skills for the existing MockAdapter scene."""

from pathlib import Path

from skills.base_skill import Skill, SkillResult
from sim.mock_tasks import SKILL_NAMES


class MockTaskSkill(Skill):
    skill_type = "hard"
    skill_level = "basic"
    robot_type = ["UAV"]
    cost = 0.1
    input_schema = {"target_id": "A target in this agent's local task options, when needed",
                    "symbol": "Communication symbol 0..3, when needed"}
    output_schema = {"robot_id": "Actuated body", "persistent": "Local motion continues between decisions"}

    def execute(self, input_data):
        from adapters.adapter_manager import get_adapter
        adapter = get_adapter()
        task = getattr(adapter, "mock_task", None)
        if not task or task.status != "running":
            return SkillResult(success=False, error_msg="No active Mock task")
        rid = adapter.get_active_robot()
        if input_data.get("robot_id", rid) != rid or rid not in task.roles:
            return SkillResult(success=False, error_msg="Task skill is bound to its own robot")
        parameters = {key: input_data[key] for key in ("target_id", "symbol") if key in input_data}
        try:
            return SkillResult(success=True, output=task.apply(rid, self.name, parameters, adapter))
        except (KeyError, ValueError, TypeError, RuntimeError) as exc:
            return SkillResult(success=False, error_msg=str(exc))


def task_skill_factories():
    doc = str(Path(__file__).parent / "docs" / "mock_tasks" / "skill.md")
    return [type("Mock_" + name, (MockTaskSkill,), {
        "name": name, "description": "Mock task: " + name.replace("_", " ") + ". Uses only the bound agent's local task options.",
        "doc_path": doc,
    }) for name in sorted(SKILL_NAMES)]
