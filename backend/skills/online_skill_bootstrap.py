"""Runtime integration for safe, executable online composite Skills."""

from __future__ import annotations

from copy import deepcopy
import json
import re
import sys
import threading


_INSTALLED = False


def _server_module():
    for name in ("server", "backend.server", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "app") and hasattr(module, "state"):
            return module
    return None


def _component_names(web) -> set[str]:
    from skills.composite_skill import OnlineCompositeSkill

    names = set()
    for registry in web.state.robot_registries.values():
        for metadata in registry.list_skills():
            name = str(metadata.get("name") or "")
            skill = registry.get_skill(name)
            if (
                name
                and name != "create_composite_skill"
                and not isinstance(skill, OnlineCompositeSkill)
            ):
                names.add(name)
    return names


def _register_definition(web, definition: dict) -> list[str]:
    from skills.composite_skill import OnlineCompositeSkill

    supported = set(definition.get("robot_type") or ["UAV"])
    world = web.state.get_world_snapshot().get("robots", {})
    registered = []
    for robot_id, registry in web.state.robot_registries.items():
        robot_type = str(world.get(robot_id, {}).get("robot_type", "UAV")).upper()
        if robot_type not in supported:
            continue
        if registry.get_skill(definition["name"]) is not None:
            raise ValueError(
                f"Skill '{definition['name']}' is already registered for {robot_id}"
            )
        previous = registry.auto_generate_doc
        registry.auto_generate_doc = False
        try:
            registry.register_skill(OnlineCompositeSkill(definition))
        finally:
            registry.auto_generate_doc = previous
        registered.append(robot_id)
    return registered


def _create_definition(web, definition: dict) -> dict:
    from skills.composite_skill_manager import get_composite_skill_manager

    if not web.state.initialized:
        raise RuntimeError("System must be initialized before creating an executable Skill")
    manager = get_composite_skill_manager()
    normalized = manager.create_skill(
        definition,
        allowed_skills=_component_names(web),
    )
    registered = []
    try:
        registered = _register_definition(web, normalized)
    except Exception:
        for registry in web.state.robot_registries.values():
            registry.unregister_skill(normalized["name"])
        manager.remove_skill(normalized["name"])
        raise
    web.state.push_log(
        "success",
        f"Online executable Skill registered: {normalized['name']} "
        f"({len(registered)} robot(s))",
    )
    web.socketio.emit("skill_catalog", web._get_skill_catalog())
    return {
        "ok": True,
        "name": normalized["name"],
        "definition": normalized,
        "registered_robots": registered,
    }


def _remove_definition(web, name: str) -> bool:
    from skills.composite_skill_manager import get_composite_skill_manager

    manager = get_composite_skill_manager()
    removed = manager.remove_skill(name)
    for registry in web.state.robot_registries.values():
        registry.unregister_skill(name)
    if removed:
        web.state.push_log("info", f"Online executable Skill removed: {name}")
        web.socketio.emit("skill_catalog", web._get_skill_catalog())
    return removed



def _install_execution_scope(web) -> None:
    """Reserve every UAV used by a composite that wraps a swarm Skill."""
    from skills.composite_skill_manager import get_composite_skill_manager

    if getattr(web, "_online_composite_scope_hook", False):
        return
    original = web._execution_robot_ids

    def execution_robot_ids(robot_id: str, skill_name: str, parameters: dict):
        definition = get_composite_skill_manager().get_definition(skill_name)
        if definition:
            swarm_step = next(
                (
                    step
                    for step in definition.get("steps", [])
                    if step.get("skill") in web._SWARM_SKILL_NAMES
                ),
                None,
            )
            if swarm_step:
                merged = dict(definition.get("defaults") or {})
                merged.update(parameters or {})
                return original(robot_id, swarm_step["skill"], merged)
        return original(robot_id, skill_name, parameters)

    web._execution_robot_ids = execution_robot_ids
    web._online_composite_scope_hook = True

def _parse_json_object(raw) -> dict:
    cleaned = str(raw or "").strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise ValueError("The model did not return a JSON definition")
        value = json.loads(match.group())
    if not isinstance(value, dict):
        raise ValueError("The generated definition must be a JSON object")
    return value


def _generate_definition(web, name: str, requirement: str) -> dict:
    from llm_client import get_client

    allowed = _component_names(web)
    catalog = []
    for registry in web.state.robot_registries.values():
        for entry in registry.get_skill_catalog():
            if entry.get("name") in allowed:
                catalog.append({
                    "name": entry.get("name"),
                    "description": entry.get("description"),
                    "input_schema": entry.get("input_schema") or {},
                })
        if catalog:
            break
    prompt = (
        "Create one safe executable composite Skill definition.\n"
        f"Requested name: {name}\n"
        f"Requirement: {requirement}\n\n"
        "Available component skills:\n"
        f"{json.dumps(catalog, ensure_ascii=False)}\n\n"
        "Return only one JSON object containing name, description, robot_type, "
        "input_schema, defaults, steps, terminal_on_success, and completion_summary. "
        "Each of the 1-12 steps contains skill, robot, and parameters. Use only exact "
        "component skill names above. Use $robot_id for the selected robot and exact "
        "$input.field strings for public inputs. Prefer one existing swarm skill for a "
        "swarm operation. Never include Python, shell, imports, URLs, source code, file "
        "operations, or network operations."
    )
    raw = get_client(module="planner").chat(
        [
            {
                "role": "system",
                "content": "Design validated JSON recipes from registered robot skills.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1400,
    )
    definition = _parse_json_object(raw)
    definition["name"] = name
    definition.setdefault("description", requirement)
    return definition


def _install_registry_hook(web) -> None:
    from skills.composite_skill import (
        CreateCompositeSkill,
        OnlineCompositeSkill,
        configure_composite_creator,
    )
    from skills.composite_skill_manager import get_composite_skill_manager
    from skills.registry import SkillRegistry

    if getattr(SkillRegistry, "_online_composite_hook", False):
        return

    original_init = SkillRegistry.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        previous = self.auto_generate_doc
        self.auto_generate_doc = False
        try:
            self.register_skill(CreateCompositeSkill())
            for definition in get_composite_skill_manager().list_definitions():
                self.register_skill(OnlineCompositeSkill(definition))
        finally:
            self.auto_generate_doc = previous

    def unregister_skill(self, name: str) -> bool:
        return self._registry.pop(str(name or "").strip(), None) is not None

    SkillRegistry.__init__ = patched_init
    SkillRegistry.unregister_skill = unregister_skill
    SkillRegistry._online_composite_hook = True
    configure_composite_creator(lambda definition: _create_definition(web, definition))


def _install_runtime_hook() -> None:
    from runtime.agent_runtime import AgentRuntime
    from skills.composite_skill import configure_composite_dispatcher

    if getattr(AgentRuntime, "_online_composite_hook", False):
        return
    original_init = AgentRuntime.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        configure_composite_dispatcher(self.dispatch_skill)

    AgentRuntime.__init__ = patched_init
    AgentRuntime._online_composite_hook = True


def _cross_offsets(count: int, spacing: float):
    spacing = max(float(spacing), 4.0)
    if count < 1:
        return []
    raw = [(0.0, 0.0)]
    directions = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))
    for index in range(1, count):
        direction_n, direction_e = directions[(index - 1) % 4]
        layer = (index - 1) // 4 + 1
        raw.append((direction_n * layer * spacing, direction_e * layer * spacing))
    mean_n = sum(point[0] for point in raw) / count
    mean_e = sum(point[1] for point in raw) / count
    return [(point[0] - mean_n, point[1] - mean_e) for point in raw]


def _install_cross_formation() -> None:
    from skills import swarm_skills

    if getattr(swarm_skills, "_online_cross_hook", False):
        return
    original_offsets = swarm_skills.formation_offsets
    original_search = swarm_skills.SwarmAreaSearch.execute
    cross_context = threading.local()

    def formation_offsets(count, formation, spacing):
        kind = str(formation or "").strip().lower()
        if kind in {"cross", "plus"} or (
            kind == "line" and getattr(cross_context, "active", False)
        ):
            return _cross_offsets(count, spacing)
        return original_offsets(count, formation, spacing)

    def search_execute(self, input_data):
        requested = str((input_data or {}).get("formation") or "").strip().lower()
        if requested not in {"cross", "plus", "\u5341\u5b57", "\u5341\u5b57\u5f62"}:
            return original_search(self, input_data)
        translated = deepcopy(input_data or {})
        translated["formation"] = "line"
        cross_context.active = True
        try:
            result = original_search(self, translated)
        finally:
            cross_context.active = False
        if getattr(result, "success", False):
            output = result.output or {}
            output["formation"] = "cross"
            output["formation_preserved"] = True
            if output.get("completion_summary"):
                output["completion_summary"] = str(output["completion_summary"]).replace(
                    "line formation", "cross formation"
                )
            if output.get("completion_summary_zh"):
                output["completion_summary_zh"] = str(
                    output["completion_summary_zh"]
                ).replace(
                    "\u76f4\u7ebf \u7f16\u961f", "\u5341\u5b57\u5f62\u7f16\u961f"
                ).replace(
                    "\u76f4\u7ebf\u7f16\u961f", "\u5341\u5b57\u5f62\u7f16\u961f"
                )
        return result

    swarm_skills.formation_offsets = formation_offsets
    swarm_skills.SwarmAreaSearch.execute = search_execute
    schema = swarm_skills.SwarmAreaSearch.input_schema
    schema["formation"] = "coverage | triangle | cross | circle | line | v"
    swarm_skills._online_cross_hook = True


def _install_agent_guards() -> None:
    from brain import agent_loop

    AgentLoop = agent_loop.AgentLoop
    if getattr(AgentLoop, "_online_composite_hook", False):
        return
    original_init = AgentLoop.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        callback = self.on_thinking

        def safe_thinking(iteration, output):
            if isinstance(output, dict) and output.get("action") is None:
                output = dict(output)
                output["action"] = {}
            callback(iteration, output)

        self.on_thinking = safe_thinking

    AgentLoop.__init__ = patched_init
    AgentLoop._online_composite_hook = True
    cross_pattern = re.compile(
        r"cross\s+formation|plus\s+formation|\u5341\u5b57(?:\u5f62|\u9635\u5217|\u7f16\u961f)?",
        re.IGNORECASE,
    )
    if not any(item[0] == "cross" for item in agent_loop._FORMATION_GOAL_PATTERNS):
        agent_loop._FORMATION_GOAL_PATTERNS += (("cross", cross_pattern),)
    agent_loop.AGENT_SYSTEM_PROMPT += (
        "\n\nOnline executable Skill rules:\n"
        "- When the operator explicitly asks to add or create a reusable Skill, call "
        "create_composite_skill once.\n"
        "- Composite steps may only use registered skills. Never generate Python, shell, "
        "imports, or file/network operations.\n"
        "- Do not create duplicates when an existing skill already provides the behavior.\n"
        "- A newly created Skill is available to subsequent tasks immediately."
    )


def _add_route(app, rule: str, endpoint: str, view_func, methods) -> None:
    if endpoint in app.view_functions:
        return
    from werkzeug.routing import Rule

    app.url_map.add(Rule(rule, endpoint=endpoint, methods=set(methods)))
    app.view_functions[endpoint] = view_func


def _install_routes(web) -> None:
    from flask import jsonify, request
    from skills.composite_skill_manager import get_composite_skill_manager

    def collection():
        manager = get_composite_skill_manager()
        if request.method == "GET":
            skills = manager.list_definitions()
            return jsonify({"ok": True, "count": len(skills), "skills": skills})
        if web.state.is_executing:
            return jsonify({
                "ok": False,
                "error": "Cannot change executable Skills while a mission is running",
            }), 409
        data = request.get_json(silent=True) or {}
        definition = data.get("definition") if isinstance(data.get("definition"), dict) else data
        try:
            return jsonify(_create_definition(web, definition)), 201
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    def generate():
        if web.state.is_executing:
            return jsonify({
                "ok": False,
                "error": "Cannot change executable Skills while a mission is running",
            }), 409
        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip()
        requirement = str(data.get("requirement") or "").strip()
        if not name or not requirement:
            return jsonify({"ok": False, "error": "name and requirement are required"}), 400
        try:
            definition = _generate_definition(web, name, requirement)
            return jsonify(_create_definition(web, definition)), 201
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    def detail(name):
        manager = get_composite_skill_manager()
        definition = manager.get_definition(name)
        if request.method == "GET":
            if definition is None:
                return jsonify({"ok": False, "error": "Skill not found"}), 404
            return jsonify({"ok": True, "definition": definition})
        if web.state.is_executing:
            return jsonify({
                "ok": False,
                "error": "Cannot change executable Skills while a mission is running",
            }), 409
        if definition is None:
            return jsonify({"ok": False, "error": "Skill not found"}), 404
        _remove_definition(web, name)
        return jsonify({"ok": True, "removed": name})

    _add_route(
        web.app,
        "/api/skills/composite",
        "online_composite_collection",
        collection,
        ["GET", "POST"],
    )
    _add_route(
        web.app,
        "/api/skills/composite/generate",
        "online_composite_generate",
        generate,
        ["POST"],
    )
    _add_route(
        web.app,
        "/api/skills/composite/<name>",
        "online_composite_detail",
        detail,
        ["GET", "DELETE"],
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    web = _server_module()
    if web is None:
        return
    _INSTALLED = True
    _install_registry_hook(web)
    _install_runtime_hook()
    _install_cross_formation()
    _install_agent_guards()
    _install_execution_scope(web)
    _install_routes(web)

