"""Persistent manager for validated online composite skill definitions."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import re
from typing import Iterable


logger = logging.getLogger(__name__)
COMPOSITE_SPECS_DIR = Path(__file__).parent / "composite_specs"
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class CompositeDefinitionError(ValueError):
    """Raised when an online composite definition violates the safe schema."""


def normalize_composite_definition(
    definition: dict,
    *,
    allowed_skills: Iterable[str] | None = None,
) -> dict:
    if not isinstance(definition, dict):
        raise CompositeDefinitionError("definition must be an object")

    name = str(definition.get("name") or "").strip().lower().replace("-", "_")
    if not _SKILL_NAME_RE.fullmatch(name):
        raise CompositeDefinitionError(
            "name must be snake_case and contain 3-64 lowercase characters"
        )

    description = str(definition.get("description") or "").strip()
    if not description:
        raise CompositeDefinitionError("description is required")
    if len(description) > 500:
        raise CompositeDefinitionError("description is too long")

    raw_robot_types = definition.get("robot_type") or ["UAV"]
    if not isinstance(raw_robot_types, (list, tuple)):
        raise CompositeDefinitionError("robot_type must be a list")
    robot_types = []
    for value in raw_robot_types:
        robot_type = str(value or "").strip().upper()
        if robot_type not in {"UAV", "UGV"}:
            raise CompositeDefinitionError(f"unsupported robot_type: {value}")
        if robot_type not in robot_types:
            robot_types.append(robot_type)

    input_schema = definition.get("input_schema") or {}
    defaults = definition.get("defaults") or {}
    if not isinstance(input_schema, dict) or not isinstance(defaults, dict):
        raise CompositeDefinitionError("input_schema and defaults must be objects")

    raw_steps = definition.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise CompositeDefinitionError("steps must contain at least one step")
    if len(raw_steps) > 12:
        raise CompositeDefinitionError("a composite skill supports at most 12 steps")

    allowed = set(allowed_skills) if allowed_skills is not None else None
    steps = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise CompositeDefinitionError(f"step {index} must be an object")
        component = str(raw_step.get("skill") or "").strip()
        if not _SKILL_NAME_RE.fullmatch(component):
            raise CompositeDefinitionError(f"step {index} has an invalid skill name")
        if component == name or component == "create_composite_skill":
            raise CompositeDefinitionError(
                f"step {index} cannot call the composite itself or the creator"
            )
        if allowed is not None and component not in allowed:
            raise CompositeDefinitionError(
                f"step {index} references unavailable skill '{component}'"
            )
        parameters = raw_step.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise CompositeDefinitionError(f"step {index} parameters must be an object")
        robot = str(raw_step.get("robot") or "$robot_id").strip()
        if not robot:
            raise CompositeDefinitionError(f"step {index} robot cannot be empty")
        steps.append({
            "skill": component,
            "robot": robot,
            "parameters": deepcopy(parameters),
        })

    normalized = {
        "name": name,
        "description": description,
        "robot_type": robot_types,
        "input_schema": deepcopy(input_schema),
        "defaults": deepcopy(defaults),
        "steps": steps,
        "terminal_on_success": bool(definition.get("terminal_on_success", False)),
        "completion_summary": str(
            definition.get("completion_summary")
            or f"Online composite skill {name} completed successfully."
        ).strip(),
    }
    try:
        json.dumps(normalized, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise CompositeDefinitionError(
            f"definition must be JSON serializable: {exc}"
        ) from exc
    return normalized


class CompositeSkillManager:
    def __init__(self, specs_dir: Path = COMPOSITE_SPECS_DIR):
        self._specs_dir = Path(specs_dir)
        self._specs_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict] = {}
        self.refresh()

    def refresh(self) -> None:
        self._cache.clear()
        for path in sorted(self._specs_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                definition = normalize_composite_definition(raw)
                self._cache[definition["name"]] = definition
            except Exception as exc:
                logger.warning("Ignoring invalid composite skill %s: %s", path, exc)

    def list_definitions(self) -> list[dict]:
        return [deepcopy(value) for value in self._cache.values()]

    def get_definition(self, name: str) -> dict | None:
        definition = self._cache.get(str(name or "").strip())
        return deepcopy(definition) if definition else None

    def skill_exists(self, name: str) -> bool:
        return str(name or "").strip() in self._cache

    def create_skill(
        self,
        definition: dict,
        *,
        allowed_skills: Iterable[str] | None = None,
    ) -> dict:
        normalized = normalize_composite_definition(
            definition,
            allowed_skills=allowed_skills,
        )
        name = normalized["name"]
        if name in self._cache:
            raise CompositeDefinitionError(f"composite skill '{name}' already exists")
        path = self._specs_dir / f"{name}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        self._cache[name] = normalized
        logger.info("Created online composite skill: %s", name)
        return deepcopy(normalized)

    def remove_skill(self, name: str) -> bool:
        normalized_name = str(name or "").strip()
        path = self._specs_dir / f"{normalized_name}.json"
        if path.exists():
            path.unlink()
        removed = self._cache.pop(normalized_name, None) is not None
        if removed:
            logger.info("Removed online composite skill: %s", normalized_name)
        return removed


_manager: CompositeSkillManager | None = None


def get_composite_skill_manager() -> CompositeSkillManager:
    global _manager
    if _manager is None:
        _manager = CompositeSkillManager()
    return _manager
