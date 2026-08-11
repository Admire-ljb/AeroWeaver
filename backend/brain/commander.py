"""Central mission commander for decomposing global tasks across UAV agents."""

from __future__ import annotations

import json
import math
import re

from brain.uav_agent_context import normalize_robot_id
from brain.pursuit_mission import (
    build_pursuit_initialization,
    normalize_area_bounds,
    parse_pursuit_request,
)


def _extract_json(text: str) -> dict | None:
    text = str(text or "").strip()
    candidates = []
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.I)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def _robot_summary(robot_states: dict) -> str:
    rows = []
    for robot_id, state in sorted(robot_states.items()):
        rows.append(
            f"- {robot_id}: position={state.get('position', [0, 0, 0])}, "
            f"battery={state.get('battery', '?')}%, status={state.get('status', 'idle')}, "
            f"in_air={state.get('in_air', False)}"
        )
    return "\n".join(rows)


def _commander_prompt(robot_states: dict, force_mission: bool) -> str:
    robot_ids = sorted(robot_states)
    mode_rule = (
        "The operator explicitly selected execution mode. Return type=mission."
        if force_mission
        else "Decide whether the input is a conversation/question or an executable global mission."
    )
    return f"""You are COMMANDER, the central coordinator of AeroWeaver.
You have no physical body, no skill runtime, and no authority to control a UAV.
You may initialize the pre-mission scene and assign goals, constraints, and peers only.
After assignment, each UAV independently observes, plans, and dispatches skills to its own body.
Never emit a skill name, action plan, velocity, or direct flight command for any UAV.
{mode_rule}

Active UAV agents:
{_robot_summary(robot_states)}

For conversation, return JSON only:
{{"type":"chat","reply":"your concise answer"}}

For a mission, return JSON only:
{{
  "type":"mission",
  "description":"operator-facing mission description",
  "summary":"short global strategy",
  "operator_report":"brief mission-start report in the operator's language",
  "movement_budget_m":15,
  "max_world_steps":48,
  "initialization":[
    {{"robot_id":"UAV_1","position":[0,0,-5]}}
  ],
  "metrics":[
    {{"key":"coverage","label":"Area coverage","unit":"%","target":100,"measurement":"coverage_pct"}}
  ],
  "termination_conditions":[
    {{"id":"coverage_complete","description":"The assigned search region has been covered and observations reported"}},
    {{"id":"agent_consensus","description":"Every active UAV reports no unresolved local task or safety issue"}}
  ],
  "assignments":[
    {{"robot_id":"UAV_1","task":"specific executable subtask","coordination_peers":["UAV_2"]}}
  ]
}}

Mission rules:
- Treat initialization poses as one-time scene-reset data, never as in-mission control actions.
- Assign goals and constraints, not step-by-step skill plans. UAV agents choose their own actions.
- Assign exactly one useful, non-duplicated subtask to every active UAV: {', '.join(robot_ids)}.
- Provide one distinct, collision-safe initialization pose for every UAV. Down must be negative.
- Set one shared movement_budget_m (5-40 m) for this decision round. Every assigned UAV should move that aggregate distance unless its subtask is already complete or a safety constraint requires holding.
- Keep per-UAV movement distances equal or approximately equal; the runtime will rescale translational paths to the shared budget.
- Choose 2-5 measurable, task-relevant metrics. measurement should use one of: coverage_pct, completion_pct, active_uavs, communication_links, communication_health_pct, minimum_separation_m, distance_balance_pct, world_steps.
- Define 2-5 concrete completion conditions before execution begins and set max_world_steps as a hard timeout.
- A pursuit task must identify pursuers and an evader; capture distance is the completion condition and max_world_steps is the timeout condition.
- Mission termination requires every active UAV agent to independently vote that these conditions and its own assignment are satisfied.
- The operator_report must explain the task, starting distribution, success criteria, and chosen metrics. Do not mention internal skill counts.
- Respect each UAV's current position and battery.
- Each UAV plans and controls only its own body.
- Use coordination_peers for agents that must exchange state or deconflict motion.
- Include concrete regions, relative sectors, coordinates, altitude, separation, or completion criteria when the operator supplied them.
- Do not invent unavailable UAV identifiers.
"""


def _normalize_mission(parsed: dict, task: str, robot_states: dict, task_area=None) -> dict:
    robot_ids = sorted(robot_states)
    pursuit_spec = parse_pursuit_request(task, robot_ids)
    area_bounds = normalize_area_bounds(task_area)
    if pursuit_spec and area_bounds:
        pursuit_spec["area_bounds"] = area_bounds
    if pursuit_spec:
        robot_ids = list(pursuit_spec["participants"])
    try:
        movement_budget = min(max(float(parsed.get("movement_budget_m", 15.0)), 5.0), 40.0)
    except (TypeError, ValueError):
        movement_budget = 15.0
    if pursuit_spec:
        movement_budget = round(
            float(pursuit_spec["pursuer_speed_mps"])
            * float(pursuit_spec["decision_interval_s"]),
            2,
        )

    by_robot = {}
    for assignment in parsed.get("assignments") or []:
        if not isinstance(assignment, dict):
            continue
        robot_id = normalize_robot_id(assignment.get("robot_id"))
        if robot_id not in robot_states or robot_id in by_robot:
            continue
        peers = []
        for peer in assignment.get("coordination_peers") or []:
            peer_id = normalize_robot_id(peer)
            if peer_id in robot_states and peer_id != robot_id and peer_id not in peers:
                peers.append(peer_id)
        subtask = str(assignment.get("task") or "").strip()
        if subtask:
            by_robot[robot_id] = {
                "robot_id": robot_id,
                "task": subtask,
                "coordination_peers": peers,
            }

    total = max(1, len(robot_ids))
    for index, robot_id in enumerate(robot_ids):
        if robot_id in by_robot:
            continue
        peer = robot_ids[(index + 1) % total] if total > 1 else None
        by_robot[robot_id] = {
            "robot_id": robot_id,
            "task": (
                f"Global mission: {task}\n"
                f"You are sector agent {index + 1}/{total}. Cover a distinct sector, "
                "maintain safe separation, report observations, and coordinate before crossing another sector."
            ),
            "coordination_peers": [peer] if peer else [],
        }

    initialization = {}
    accepted_positions = []
    proposed_initialization = (
        build_pursuit_initialization(pursuit_spec)
        if pursuit_spec
        else parsed.get("initialization") or []
    )
    for item in proposed_initialization:
        if not isinstance(item, dict):
            continue
        robot_id = normalize_robot_id(item.get("robot_id"))
        raw_position = list(item.get("position") or [])
        if robot_id not in robot_states or robot_id in initialization or len(raw_position) < 3:
            continue
        try:
            position = [float(value) for value in raw_position[:3]]
        except (TypeError, ValueError):
            continue
        if area_bounds:
            position[0] = min(area_bounds["north_max"], max(area_bounds["north_min"], position[0]))
            position[1] = min(area_bounds["east_max"], max(area_bounds["east_min"], position[1]))
        position[2] = min(-3.0, max(-60.0, position[2]))
        if any(sum((position[i] - other[i]) ** 2 for i in range(3)) < 25.0 for other in accepted_positions):
            continue


        initialization[robot_id] = position
        accepted_positions.append(position)

    columns = max(1, math.ceil(total ** 0.5))
    rows = max(1, math.ceil(total / columns))
    for index, robot_id in enumerate(robot_ids):
        if robot_id in initialization:
            continue
        if area_bounds:
            north_span = area_bounds["north_max"] - area_bounds["north_min"]
            east_span = area_bounds["east_max"] - area_bounds["east_min"]
            position = [
                area_bounds["north_min"] + (index // columns + 0.5) * north_span / rows,
                area_bounds["east_min"] + (index % columns + 0.5) * east_span / columns,
                -5.0,
            ]
        else:
            position = [
                float((index % columns) * 12),
                float((index // columns) * 12),
                -5.0,
            ]
        while any(sum((position[i] - other[i]) ** 2 for i in range(3)) < 25.0 for other in accepted_positions):
            if area_bounds:
                raise ValueError("The selected mission area is too small for safe UAV initialization.")
            position[0] += 12.0
        initialization[robot_id] = position
        accepted_positions.append(position)

    metric_rows = []
    allowed_measurements = {
        "coverage_pct", "completion_pct", "active_uavs", "communication_links",
        "communication_health_pct", "minimum_separation_m", "distance_balance_pct",
        "world_steps", "capture_distance_m", "round_index", "remaining_world_steps",
    }
    for metric in parsed.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        measurement = str(metric.get("measurement") or metric.get("key") or "").strip()
        if measurement not in allowed_measurements:
            continue
        key = str(metric.get("key") or measurement).strip()
        if not key or any(item["key"] == key for item in metric_rows):
            continue
        metric_rows.append({
            "key": key,
            "label": str(metric.get("label") or key.replace("_", " ").title()),
            "unit": str(metric.get("unit") or ""),
            "target": metric.get("target"),
            "measurement": measurement,
            "current": metric.get("current", 0),
        })
        if len(metric_rows) >= 5:
            break

    termination_conditions = []
    for index, condition in enumerate(parsed.get("termination_conditions") or []):
        if isinstance(condition, str):
            description = condition.strip()
            condition_id = f"condition_{index + 1}"
        elif isinstance(condition, dict):
            description = str(condition.get("description") or condition.get("condition") or "").strip()
            condition_id = str(condition.get("id") or f"condition_{index + 1}").strip()
        else:
            continue
        if not description or any(item["description"] == description for item in termination_conditions):
            continue
        normalized_condition = {"id": condition_id, "description": description}
        if isinstance(condition, dict):
            for key in ("measurement", "operator", "target", "hard"):
                if key in condition:
                    normalized_condition[key] = condition[key]
        termination_conditions.append(normalized_condition)
        if len(termination_conditions) >= 5:
            break
    if pursuit_spec:
        termination_conditions = [
            {
                "id": "capture",
                "description": (
                    f"At least one pursuer comes within {pursuit_spec['capture_radius_m']:.1f} m "
                    f"of {pursuit_spec['evader']}."
                ),
                "measurement": "capture_distance_m",
                "operator": "<=",
                "target": pursuit_spec["capture_radius_m"],
                "hard": True,
            },
            {
                "id": "step_timeout",
                "description": (
                    f"Stop without capture after {pursuit_spec['max_world_steps']} agent world steps."
                ),
                "measurement": "world_steps",
                "operator": ">=",
                "target": pursuit_spec["max_world_steps"],
                "hard": True,
            },
        ]
        metric_rows = [
            {
                "key": "capture_distance",
                "label": "Closest pursuer",
                "unit": "m",
                "target": pursuit_spec["capture_radius_m"],
                "measurement": "capture_distance_m",
                "current": 0,
            },
            {
                "key": "world_step",
                "label": "World steps",
                "unit": "",
                "target": pursuit_spec["max_world_steps"],
                "measurement": "world_steps",
                "current": 0,
            },
            {
                "key": "separation",
                "label": "Minimum separation",
                "unit": "m",
                "target": pursuit_spec["collision_radius_m"],
                "measurement": "minimum_separation_m",
                "current": 0,
            },
            {
                "key": "communications",
                "label": "Local links",
                "unit": "",
                "target": max(1, len(robot_ids) - 1),
                "measurement": "communication_links",
                "current": 0,
            },
        ]
    elif not termination_conditions:
        termination_conditions = [
            {
                "id": "assignments_complete",
                "description": "Every UAV has completed its assigned subtask and reported local evidence.",
            },
            {
                "id": "no_unresolved_risk",
                "description": "Every UAV reports no unresolved safety or coordination issue.",
            },
        ]

    budget_instruction = (
        f"\nShared decision-round movement budget: {movement_budget:.1f} m. "
        "Propose a translational path with this aggregate distance unless the subtask is "
        "already complete or safety requires holding; the runtime will enforce the same "
        "budget for every moving UAV."
    )
    if area_bounds:
        budget_instruction += (
            "\nHard mission boundary: "
            f"N=[{area_bounds['north_min']}, {area_bounds['north_max']}], "
            f"E=[{area_bounds['east_min']}, {area_bounds['east_max']}]. "
            "Every target and intermediate movement must remain inside it."
        )
    if pursuit_spec:
        for robot_id in robot_ids:
            role = "evader" if robot_id == pursuit_spec["evader"] else "pursuer"
            if role == "evader":
                subtask = (
                    f"You are the evader. Avoid {', '.join(pursuit_spec['pursuers'])}, remain inside "
                    "the mission arena, use only local observations and peer messages, and choose your "
                    "own persistent movement direction each world round."
                )
            else:
                subtask = (
                    f"You are a pursuer. Cooperatively capture {pursuit_spec['evader']} within "
                    f"{pursuit_spec['capture_radius_m']:.1f} m. Use local observations and peer messages "
                    "to choose your own persistent movement direction and avoid collisions."
                )
            by_robot[robot_id] = {
                "robot_id": robot_id,
                "task": subtask,
                "coordination_peers": [peer for peer in robot_ids if peer != robot_id],
                "role": role,
            }

    assignments = []
    for robot_id in robot_ids:
        assignment = by_robot[robot_id]
        assignments.append({
            **assignment,
            "task": assignment["task"] + budget_instruction,
            "initial_position": initialization[robot_id],
        })

    description = str(parsed.get("description") or task).strip()
    summary = str(parsed.get("summary") or "Commander distributed the global mission across all active UAVs.")
    operator_report = str(parsed.get("operator_report") or "").strip()
    if pursuit_spec:
        distribution = ", ".join(
            f"{robot_id} at {initialization[robot_id]}"
            for robot_id in robot_ids
        )
        operator_report = (
            f"Pursuit mission: {', '.join(pursuit_spec['pursuers'])} will pursue "
            f"{pursuit_spec['evader']} from randomized start poses.\n"
            f"Initial UAV distribution: {distribution}\n"
            "Each UAV keeps an isolated context, exchanges state only over current local links, "
            "and maintains its chosen velocity until it turns or the mission terminates.\n"
            f"Success: capture distance <= {pursuit_spec['capture_radius_m']:.1f} m. "
            f"Timeout: {pursuit_spec['max_world_steps']} agent world steps."
        )
    elif not operator_report:
        distribution = ", ".join(
            f"{robot_id} at {initialization[robot_id]}"
            for robot_id in robot_ids
        )
        operator_report = (
            f"Mission: {description}\n"
            f"Initial UAV distribution: {distribution}\n"
            f"Strategy: {summary}\n"
            f"Shared movement budget: {movement_budget:.1f} m per moving agent decision.\n"
            "Termination conditions: "
            + "; ".join(item["description"] for item in termination_conditions)
        )

    try:
        generic_max_world_steps = int(parsed.get("max_world_steps", max(12, total * 6)))
    except (TypeError, ValueError):
        generic_max_world_steps = max(12, total * 6)
    generic_max_world_steps = min(max(generic_max_world_steps, total), 240)

    return {
        "type": "mission",
        "description": description,
        "summary": summary,
        "operator_report": operator_report,
        "movement_budget_m": movement_budget,
        "max_world_steps": int(
            pursuit_spec["max_world_steps"]
            if pursuit_spec
            else generic_max_world_steps
        ),
        "scenario": pursuit_spec or ({"type": "generic", "area_bounds": area_bounds} if area_bounds else {}),
        "task_area": area_bounds,
        "initialization": [
            {"robot_id": robot_id, "position": initialization[robot_id]}
            for robot_id in robot_ids
        ],
        "metrics": metric_rows,
        "termination_conditions": termination_conditions,
        "assignments": assignments,
    }


def handle_global_input(
    user_input: str,
    history: list[dict],
    llm_client,
    robot_states: dict,
    *,
    force_mission: bool = False,
    conversation_only: bool = False,
    task_area=None,
) -> dict:
    """Return either a Commander reply or one assignment per active UAV."""
    system_prompt = _commander_prompt(robot_states, force_mission and not conversation_only)
    if conversation_only:
        system_prompt += "\nConversation-only mode is active. Always return type=chat and do not dispatch a mission."
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(
        {"role": item["role"], "content": item["content"]}
        for item in history[-16:]
        if item.get("role") in {"user", "assistant"} and item.get("content")
    )
    messages.append({"role": "user", "content": user_input})

    raw = llm_client.chat(messages, temperature=0.4, max_tokens=1600)
    parsed = _extract_json(raw) or {}
    pursuit_requested = parse_pursuit_request(user_input, robot_states)
    if conversation_only or (
        parsed.get("type") == "chat" and not force_mission and not pursuit_requested
    ):
        reply = str(parsed.get("reply") or raw or "Commander is ready.").strip()
        return {"type": "chat", "reply": reply, "assignments": []}
    return _normalize_mission(parsed, user_input, robot_states, task_area=task_area)


def build_progress_report(mission_snapshot: dict, llm_client, phase: str) -> str:
    """Turn structured mission telemetry into a concise operator-facing briefing."""
    prompt = """You are AeroWeaver COMMANDER reporting mission progress to an operator.
Use the same language as the mission description. Report only operationally meaningful facts:
- current task and strategy;
- UAV spatial distribution and per-UAV role/status;
- task-relevant metric values versus targets;
- the declared termination conditions and each UAV's READY/CONTINUE vote;
- risks, deviations, and the next coordination action.
Never report the mission as complete unless termination_consensus is true.
Do not mention skill step counts, replanning counts, prompts, JSON, or internal implementation.
Use 3-6 concise sentences."""
    payload = {
        "phase": str(phase),
        "mission": mission_snapshot,
    }
    raw = llm_client.chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.3,
        max_tokens=700,
    )
    parsed = _extract_json(raw)
    if parsed and parsed.get("reply"):
        return str(parsed["reply"]).strip()
    return str(raw or "").strip()
