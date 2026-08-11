"""Pursuit-evasion mission semantics for locally communicating UAV agents."""

from __future__ import annotations

import json
import math
import random
import re
from copy import deepcopy

from brain.uav_agent_context import normalize_robot_id


PURSUIT_KEYWORDS = re.compile(r"(?:追逐|追踪|追捕|围捕|chase|pursu|hunt)", re.I)
_RANGE_PATTERN = re.compile(
    r"UAV[_\s-]?(\d+)\s*(?:-|~|至|到)\s*(?:UAV[_\s-]?)?(\d+)",
    re.I,
)
_UAV_PATTERN = re.compile(r"UAV[_\s-]?(\d+)", re.I)


def normalize_area_bounds(value) -> dict | None:
    """Validate a rectangular N/E mission boundary from UI or mission data."""
    if not isinstance(value, dict):
        return None
    aliases = {
        "north_min": ("north_min", "northMin"),
        "north_max": ("north_max", "northMax"),
        "east_min": ("east_min", "eastMin"),
        "east_max": ("east_max", "eastMax"),
    }
    normalized = {}
    try:
        for target, keys in aliases.items():
            raw = next(value[key] for key in keys if key in value)
            normalized[target] = float(raw)
    except (StopIteration, TypeError, ValueError):
        return None
    if (
        normalized["north_max"] - normalized["north_min"] < 2.0
        or normalized["east_max"] - normalized["east_min"] < 2.0
    ):
        return None
    return {key: round(number, 3) for key, number in normalized.items()}


def position_inside_area(position, bounds, *, tolerance=1e-6) -> bool:
    area = normalize_area_bounds(bounds)
    if not area:
        return True
    point = _vector(position)
    return (
        area["north_min"] - tolerance <= point[0] <= area["north_max"] + tolerance
        and area["east_min"] - tolerance <= point[1] <= area["east_max"] + tolerance
    )


def constrain_direction_to_area(position, direction, speed_mps, interval_s, bounds) -> list[float]:
    """Turn an outward command inward before the physics hard boundary clips it."""
    area = normalize_area_bounds(bounds)
    desired = _unit(direction)
    if not area:
        return desired
    own = _vector(position)
    margin = min(
        1.0,
        (area["north_max"] - area["north_min"]) / 4.0,
        (area["east_max"] - area["east_min"]) / 4.0,
    )
    predicted = [
        own[0] + desired[0] * float(speed_mps) * float(interval_s),
        own[1] + desired[1] * float(speed_mps) * float(interval_s),
    ]
    target = [
        min(area["north_max"] - margin, max(area["north_min"] + margin, predicted[0])),
        min(area["east_max"] - margin, max(area["east_min"] + margin, predicted[1])),
    ]
    correction = [target[0] - own[0], target[1] - own[1], 0.0]
    if math.hypot(correction[0], correction[1]) <= 1e-6:
        correction = [
            (area["north_min"] + area["north_max"]) / 2.0 - own[0],
            (area["east_min"] + area["east_max"]) / 2.0 - own[1],
            0.0,
        ]
    return _unit(correction, fallback=desired)


def _vector(values) -> list[float]:
    result = [float(value) for value in list(values or [])[:3]]
    return (result + [0.0, 0.0, 0.0])[:3]


def _distance(a, b) -> float:
    first, second = _vector(a), _vector(b)
    return math.sqrt(sum((first[index] - second[index]) ** 2 for index in range(3)))


def _unit(values, fallback=(1.0, 0.0, 0.0)) -> list[float]:
    vector = _vector(values)
    vector[2] = 0.0
    length = math.hypot(vector[0], vector[1])
    if length <= 1e-9:
        vector = _vector(fallback)
        length = max(math.hypot(vector[0], vector[1]), 1.0)
    return [vector[0] / length, vector[1] / length, 0.0]


def parse_pursuit_request(text: str, available_robot_ids) -> dict | None:
    """Extract ``UAV1-3 chase UAV4`` style semantics without relying on the LLM."""
    task = str(text or "")
    if not PURSUIT_KEYWORDS.search(task):
        return None

    available = {normalize_robot_id(item) for item in available_robot_ids}
    keyword = PURSUIT_KEYWORDS.search(task)
    before = task[: keyword.start()] if keyword else task
    after = task[keyword.end() :] if keyword else ""

    pursuers = []
    range_match = _RANGE_PATTERN.search(before)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        step = 1 if end >= start else -1
        pursuers = [f"UAV_{index}" for index in range(start, end + step, step)]
    else:
        pursuers = [f"UAV_{value}" for value in _UAV_PATTERN.findall(before)]

    after_ids = [f"UAV_{value}" for value in _UAV_PATTERN.findall(after)]
    all_ids = [f"UAV_{value}" for value in _UAV_PATTERN.findall(task)]
    evader = after_ids[0] if after_ids else (all_ids[-1] if len(all_ids) > 1 else "")
    if not pursuers and evader:
        pursuers = [robot_id for robot_id in all_ids if robot_id != evader]

    pursuers = [
        robot_id for robot_id in dict.fromkeys(pursuers)
        if robot_id in available and robot_id != evader
    ]
    if evader not in available or not pursuers:
        return None

    participants = pursuers + [evader]
    max_rounds = max(12, min(24, 12 + len(participants) * 2))
    return {
        "type": "pursuit",
        "pursuers": pursuers,
        "evader": evader,
        "participants": participants,
        "capture_radius_m": 6.0,
        "max_rounds": max_rounds,
        "max_world_steps": max_rounds * len(participants),
        "decision_interval_s": 1.5,
        "pursuer_speed_mps": 7.0,
        "evader_speed_mps": 6.2,
        "communication_range_m": 55.0,
        "sensor_range_m": 85.0,
        "collision_radius_m": 5.0,
        "arena_radius_m": 75.0,
    }


def build_pursuit_initialization(spec: dict, *, rng=None) -> list[dict]:
    """Create a visible, collision-safe randomized pursuit starting layout."""
    rng = rng or random.SystemRandom()
    pursuers = list(spec.get("pursuers") or [])
    evader = str(spec.get("evader") or "")
    if not pursuers or not evader:
        return []

    area = normalize_area_bounds(spec.get("area_bounds"))
    if area:
        width_n = area["north_max"] - area["north_min"]
        width_e = area["east_max"] - area["east_min"]
        required_span = max(12.0, float(spec.get("capture_radius_m", 6.0)) * 2.2)
        if min(width_n, width_e) < required_span:
            raise ValueError(
                f"Mission area is too small for collision-safe pursuit initialization; "
                f"minimum side must be at least {required_span:.1f} m."
            )
        center_n = (area["north_min"] + area["north_max"]) / 2.0
        center_e = (area["east_min"] + area["east_max"]) / 2.0
        max_radius = max(6.0, min(width_n, width_e) * 0.34)
        min_radius = min(max_radius, max(7.0, float(spec.get("capture_radius_m", 6.0)) * 1.35))
    else:
        center_n = rng.uniform(-8.0, 8.0)
        center_e = rng.uniform(-8.0, 8.0)
        min_radius, max_radius = 15.0, 19.0
    altitude = -rng.uniform(8.0, 12.0)
    positions = {
        evader: [round(center_n, 2), round(center_e, 2), round(altitude, 2)],
    }
    count = len(pursuers)
    arc_start = math.radians(90.0)
    arc_span = math.radians(180.0)
    for index, robot_id in enumerate(pursuers):
        fraction = 0.5 if count == 1 else index / (count - 1)
        angle = arc_start + arc_span * fraction + rng.uniform(-0.12, 0.12)
        radius = rng.uniform(min_radius, max_radius)
        positions[robot_id] = [
            round(center_n + math.cos(angle) * radius, 2),
            round(center_e + math.sin(angle) * radius, 2),
            round(altitude + rng.uniform(-0.8, 0.8), 2),
        ]

    initialization = [
        {"robot_id": robot_id, "position": positions[robot_id]}
        for robot_id in [*pursuers, evader]
    ]
    if area and not all(position_inside_area(item["position"], area) for item in initialization):
        raise ValueError("Pursuit initialization could not fit inside the selected mission area.")
    return initialization


def pursuit_links(positions: dict, participants, communication_range_m: float) -> list[tuple[str, str]]:
    """Return the undirected links currently available under the local range limit."""
    agents = [robot_id for robot_id in participants if robot_id in positions]
    links = []
    for index, source in enumerate(agents):
        for target in agents[index + 1 :]:
            if _distance(positions[source], positions[target]) <= float(communication_range_m):
                links.append((source, target))
    return links


def build_local_observation(
    robot_id: str,
    positions: dict,
    velocities: dict,
    spec: dict,
) -> dict:
    """Build one POMDP observation containing only locally sensed UAV state."""
    own_position = _vector(positions.get(robot_id))
    sensor_range = float(spec.get("sensor_range_m", 85.0))
    communication_range = float(spec.get("communication_range_m", 55.0))
    observed = {}
    communicable = []
    for peer_id in spec.get("participants") or []:
        if peer_id == robot_id or peer_id not in positions:
            continue
        distance = _distance(own_position, positions[peer_id])
        if distance <= sensor_range:
            observed[peer_id] = {
                "position": _vector(positions[peer_id]),
                "velocity": _vector(velocities.get(peer_id)),
                "distance_m": round(distance, 2),
                "role": "evader" if peer_id == spec.get("evader") else "pursuer",
            }
        if distance <= communication_range:
            communicable.append(peer_id)
    return {
        "robot_id": robot_id,
        "role": "evader" if robot_id == spec.get("evader") else "pursuer",
        "position": own_position,
        "velocity": _vector(velocities.get(robot_id)),
        "observed_uavs": observed,
        "communicable_peers": sorted(communicable),
        "capture_radius_m": float(spec.get("capture_radius_m", 5.0)),
        "arena_radius_m": float(spec.get("arena_radius_m", 75.0)),
        "area_bounds": normalize_area_bounds(spec.get("area_bounds")),
    }


def tactical_direction(observation: dict, spec: dict) -> list[float]:
    """Mission envelope used when LLM output is absent or tactically unsafe."""
    own = _vector(observation.get("position"))
    observed = dict(observation.get("observed_uavs") or {})
    role = str(observation.get("role") or "pursuer")
    evader = str(spec.get("evader") or "")

    if role == "pursuer" and evader in observed:
        target = observed[evader]
        lead_seconds = 2.0
        aim = [
            target["position"][axis] + target["velocity"][axis] * lead_seconds
            for axis in range(3)
        ]
        desired = [aim[0] - own[0], aim[1] - own[1], 0.0]
    elif role == "evader":
        threats = [item for item in observed.values() if item.get("role") == "pursuer"]
        if threats:
            center = [
                sum(item["position"][axis] for item in threats) / len(threats)
                for axis in range(3)
            ]
            desired = [own[0] - center[0], own[1] - center[1], 0.0]
        else:
            desired = [1.0, 0.25, 0.0]
    else:
        peer_positions = [item["position"] for item in observed.values()]
        if peer_positions:
            center = [sum(item[axis] for item in peer_positions) / len(peer_positions) for axis in range(3)]
            desired = [center[0] - own[0], center[1] - own[1], 0.0]
        else:
            desired = observation.get("velocity") or [1.0, 0.0, 0.0]

    collision_radius = float(spec.get("collision_radius_m", 5.0))
    for item in observed.values():
        distance = float(item.get("distance_m") or 0.0)
        if 1e-6 < distance < collision_radius * 1.8:
            strength = (collision_radius * 1.8 - distance) / (collision_radius * 1.8)
            desired[0] += (own[0] - item["position"][0]) / distance * strength * 3.0
            desired[1] += (own[1] - item["position"][1]) / distance * strength * 3.0

    arena_radius = float(spec.get("arena_radius_m", 75.0))
    radial = math.hypot(own[0], own[1])
    if radial > arena_radius * 0.82:
        correction = min(3.0, (radial - arena_radius * 0.82) / max(arena_radius * 0.18, 1.0) * 3.0)
        desired[0] -= own[0] / radial * correction
        desired[1] -= own[1] / radial * correction
    direction = _unit(desired)
    return constrain_direction_to_area(
        own,
        direction,
        float(spec.get("evader_speed_mps", 6.2) if role == "evader" else spec.get("pursuer_speed_mps", 7.0)),
        float(spec.get("decision_interval_s", 1.5)),
        spec.get("area_bounds"),
    )


def motion_decision_prompt(robot_id: str, observation: dict, spec: dict, previous_direction) -> str:
    """Prompt one UAV for its next local motion decision, not a global plan."""
    return f"""You are {robot_id}, one independent motion agent in a pursuit-evasion mission.
You may reason only from the supplied local observation and messages in your own context.
Choose your own horizontal NED direction. Keep the previous direction when it remains useful;
the vehicle will continue moving without another command until you deliberately turn or the mission ends.
Communicate a concise state/intent update to locally reachable peers.

Return JSON only:
{{"direction":[north,east],"message":"local observation and intended maneuver","request_states":["UAV_2"]}}

Mission parameters: {json.dumps(spec, ensure_ascii=False)}
Previous direction: {json.dumps(list(previous_direction or []))}
Local observation: {json.dumps(observation, ensure_ascii=False)}
"""


def parse_motion_decision(raw: str, observation: dict, spec: dict) -> dict:
    """Validate an agent decision and blend it with collision/capture constraints."""
    parsed = {}
    text = str(raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                parsed = value
        except json.JSONDecodeError:
            pass

    tactical = tactical_direction(observation, spec)
    raw_direction = parsed.get("direction")
    try:
        proposed = _unit([float(raw_direction[0]), float(raw_direction[1]), 0.0])
        combined = _unit([
            tactical[0] * 0.65 + proposed[0] * 0.35,
            tactical[1] * 0.65 + proposed[1] * 0.35,
            0.0,
        ])
        source = "llm+tactical"
    except (TypeError, ValueError, IndexError):
        combined = tactical
        source = "tactical_fallback"

    combined = constrain_direction_to_area(
        observation.get("position"),
        combined,
        float(spec.get("evader_speed_mps", 6.2) if observation.get("role") == "evader" else spec.get("pursuer_speed_mps", 7.0)),
        float(spec.get("decision_interval_s", 1.5)),
        spec.get("area_bounds"),
    )

    requested = []
    reachable = set(observation.get("communicable_peers") or [])
    for peer in parsed.get("request_states") or []:
        normalized = normalize_robot_id(peer)
        if normalized in reachable and normalized not in requested:
            requested.append(normalized)
    role = observation.get("role") or "agent"
    message = str(parsed.get("message") or "").strip()
    if not message:
        message = f"{observation.get('robot_id')} continuing {role} maneuver on local observation."
    speed = (
        float(spec.get("evader_speed_mps", 6.2))
        if role == "evader"
        else float(spec.get("pursuer_speed_mps", 7.0))
    )
    return {
        "direction": [round(combined[0], 4), round(combined[1], 4), 0.0],
        "speed_mps": round(speed, 3),
        "message": message[:300],
        "request_states": requested,
        "source": source,
    }


def direction_changed(previous, current, cosine_tolerance: float = 0.997) -> bool:
    if not previous:
        return True
    first, second = _unit(previous), _unit(current)
    return first[0] * second[0] + first[1] * second[1] < float(cosine_tolerance)


def evaluate_pursuit(positions: dict, spec: dict, *, round_index: int, world_step: int) -> dict:
    pursuers = [robot_id for robot_id in spec.get("pursuers") or [] if robot_id in positions]
    evader = str(spec.get("evader") or "")
    if evader not in positions or not pursuers:
        return {"status": "timeout", "reason": "Required pursuit participants are unavailable."}

    distances = {
        robot_id: _distance(positions[robot_id], positions[evader])
        for robot_id in pursuers
    }
    closest_id = min(distances, key=distances.get)
    closest_distance = distances[closest_id]
    evidence = {
        "closest_pursuer": closest_id,
        "closest_distance_m": round(closest_distance, 2),
        "capture_radius_m": float(spec.get("capture_radius_m", 5.0)),
        "round_index": int(round_index),
        "world_step": int(world_step),
    }
    if closest_distance <= float(spec.get("capture_radius_m", 5.0)):
        return {
            "status": "complete",
            "reason": f"{closest_id} reached capture distance from {evader}.",
            "evidence": evidence,
        }
    if (
        int(round_index) >= int(spec.get("max_rounds", 16))
        or int(world_step) >= int(spec.get("max_world_steps", 64))
    ):
        return {
            "status": "timeout",
            "reason": "The pursuit reached its configured world-step limit before capture.",
            "evidence": evidence,
        }
    return {"status": "running", "reason": "Pursuit continues.", "evidence": evidence}


def scenario_copy(spec: dict | None) -> dict:
    return deepcopy(spec or {})


__all__ = [
    "build_local_observation",
    "build_pursuit_initialization",
    "constrain_direction_to_area",
    "direction_changed",
    "evaluate_pursuit",
    "motion_decision_prompt",
    "parse_motion_decision",
    "parse_pursuit_request",
    "normalize_area_bounds",
    "position_inside_area",
    "pursuit_links",
    "scenario_copy",
    "tactical_direction",
]
