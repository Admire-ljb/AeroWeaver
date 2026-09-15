"""Executable online composite skills built from validated registered skills."""

from __future__ import annotations

from copy import deepcopy
import json
import time
from typing import Callable

from skills.base_skill import Skill, SkillResult


_dispatch_step: Callable[[dict], object] | None = None
_create_skill: Callable[[dict], dict] | None = None


def configure_composite_dispatcher(callback: Callable[[dict], object] | None) -> None:
    global _dispatch_step
    _dispatch_step = callback


def configure_composite_creator(callback: Callable[[dict], dict] | None) -> None:
    global _create_skill
    _create_skill = callback


def _resolve_reference(value, context):
    if isinstance(value, list):
        return [_resolve_reference(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_reference(item, context) for key, item in value.items()}
    if not isinstance(value, str) or not value.startswith("$"):
        return deepcopy(value)
    if value == "$robot_id":
        return context["robot_id"]
    if value.startswith("$input."):
        current = context["input"]
        for key in value[len("$input."):].split("."):
            if not isinstance(current, dict) or key not in current:
                raise ValueError(f"missing composite input reference: {value}")
            current = current[key]
        return deepcopy(current)
    if value.startswith("$steps."):
        current = context["steps"]
        for key in value[len("$steps."):].split("."):
            if isinstance(current, list) and key.isdigit() and int(key) < len(current):
                current = current[int(key)]
            elif isinstance(current, dict) and key in current:
                current = current[key]
            else:
                raise ValueError(f"missing composite step reference: {value}")
        return deepcopy(current)
    raise ValueError(f"unsupported composite reference: {value}")


class OnlineCompositeSkill(Skill):
    skill_type = "soft"
    skill_level = "advanced"
    preconditions = []
    output_schema = {
        "step_results": "ordered component execution results",
        "completion_summary": "terminal summary when configured",
    }

    def __init__(self, definition: dict):
        self.definition = deepcopy(definition)
        self.name = definition["name"]
        self.description = definition["description"]
        self.robot_type = list(definition.get("robot_type") or ["UAV"])
        self.input_schema = deepcopy(definition.get("input_schema") or {})
        self.defaults = deepcopy(definition.get("defaults") or {})
        self.steps = deepcopy(definition.get("steps") or [])
        self.terminal_on_success = bool(definition.get("terminal_on_success", False))
        self.cost = max(1.0, float(len(self.steps)))

    def get_catalog_entry(self) -> dict:
        entry = super().get_catalog_entry()
        entry.update({
            "online": True,
            "executable": True,
            "step_count": len(self.steps),
        })
        return entry

    def execute(self, input_data: dict) -> SkillResult:
        started = time.time()
        if _dispatch_step is None:
            return SkillResult(success=False, error_msg="Composite dispatcher is unavailable")

        robot_id = str(input_data.get("robot_id") or "UAV_1")
        merged_input = deepcopy(self.defaults)
        merged_input.update({
            key: deepcopy(value)
            for key, value in input_data.items()
            if key not in {"robot_state", "robot_id"}
        })
        context = {"robot_id": robot_id, "input": merged_input, "steps": []}
        logs = []

        for index, step in enumerate(self.steps, start=1):
            try:
                target_robot = str(_resolve_reference(step["robot"], context))
                parameters = _resolve_reference(step.get("parameters") or {}, context)
                result = _dispatch_step({
                    "step": index,
                    "skill": step["skill"],
                    "robot": target_robot,
                    "parameters": parameters,
                })
            except Exception as exc:
                return SkillResult(
                    success=False,
                    error_msg=f"Composite step {index} could not start: {exc}",
                    cost_time=round(time.time() - started, 3),
                    logs=logs,
                )

            step_result = {
                "step": index,
                "skill": step["skill"],
                "robot": target_robot,
                "success": bool(getattr(result, "success", False)),
                "output": deepcopy(getattr(result, "output", {}) or {}),
                "error": str(getattr(result, "error_msg", "") or ""),
                "cost_time": float(getattr(result, "cost_time", 0.0) or 0.0),
            }
            context["steps"].append(step_result)
            logs.append(
                f"Composite {self.name} step {index}: {step['skill']} "
                f"{'ok' if step_result['success'] else 'failed'}"
            )
            if not step_result["success"]:
                return SkillResult(
                    success=False,
                    output={"step_results": context["steps"]},
                    error_msg=(
                        f"Composite step {index} '{step['skill']}' failed: "
                        f"{step_result['error']}"
                    ),
                    cost_time=round(time.time() - started, 3),
                    logs=logs,
                )

        summary = self.definition.get("completion_summary")
        return SkillResult(
            success=True,
            output={
                "online_composite": True,
                "step_results": context["steps"],
                "completion_summary": summary,
                "completion_summary_zh": summary,
            },
            cost_time=round(time.time() - started, 3),
            logs=logs,
        )


class CreateCompositeSkill(Skill):
    name = "create_composite_skill"
    description = (
        "Create and immediately register a reusable executable advanced skill by "
        "composing existing registered skills. No source code or shell commands are allowed."
    )
    skill_type = "soft"
    skill_level = "advanced"
    robot_type = ["UAV"]
    preconditions = []
    input_schema = {
        "name": "snake_case skill ID",
        "description": "concise behavior description",
        "input_schema": "object describing public input parameters",
        "defaults": "default input values",
        "steps": "1-12 component steps using existing skills and $input.* references",
        "terminal_on_success": "whether success completes the current task",
    }
    output_schema = {
        "name": "created skill ID",
        "registered_robots": "robots that can execute it immediately",
    }
    cost = 1.0

    def execute(self, input_data: dict) -> SkillResult:
        started = time.time()
        if _create_skill is None:
            return SkillResult(success=False, error_msg="Online skill creator is unavailable")
        steps = input_data.get("steps")
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError as exc:
                return SkillResult(success=False, error_msg=f"steps is not valid JSON: {exc}")
        definition = {
            "name": input_data.get("name"),
            "description": input_data.get("description"),
            "robot_type": input_data.get("robot_type") or ["UAV"],
            "input_schema": input_data.get("input_schema") or {},
            "defaults": input_data.get("defaults") or {},
            "steps": steps,
            "terminal_on_success": input_data.get("terminal_on_success", False),
            "completion_summary": input_data.get("completion_summary"),
        }
        try:
            result = _create_skill(definition)
        except Exception as exc:
            return SkillResult(
                success=False,
                error_msg=str(exc),
                cost_time=round(time.time() - started, 3),
            )
        return SkillResult(
            success=bool(result.get("ok")),
            output=result,
            error_msg=str(result.get("error") or ""),
            cost_time=round(time.time() - started, 3),
            logs=[f"Online composite skill created: {result.get('name', '')}"],
        )
