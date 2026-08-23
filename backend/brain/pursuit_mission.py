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
_SPEED_RATIO_PATTERN = re.compile(
    r"UAV[_\s-]?(\d+)[^0-9\r\n]{0,40}?(?:速度|speed)"
    r"[^0-9\r\n]{0,24}?(\d+(?:\.\d+)?)\s*(?:倍|x|times)",
    re.I,
)
_SPEED_RATIO_SUFFIX_PATTERN = re.compile(
    r"UAV[_\s-]?(\d+)[^0-9\r\n]{0,32}?"
    r"(一|两|二|三|四|五|半|\d+(?:\.\d+)?)\s*倍"
    r"[^0-9\r\n]{0,12}?(?:速度|speed)",
    re.I,
)


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
    pursuer_speed_mps = 14.0
    evader_speed_mps = 9.0
    speed_mps_by_robot = {}
    speed_matches = [
        *_SPEED_RATIO_PATTERN.finditer(task),
        *_SPEED_RATIO_SUFFIX_PATTERN.finditer(task),
    ]
    chinese_multipliers = {
        "半": 0.5,
        "一": 1.0,
        "两": 2.0,
        "二": 2.0,
        "三": 3.0,
        "四": 4.0,
        "五": 5.0,
    }
    for match in speed_matches:
        robot_id = f"UAV_{int(match.group(1))}"
        if robot_id not in available:
            continue
        try:
            raw_multiplier = str(match.group(2)).strip()
            multiplier = (
                chinese_multipliers[raw_multiplier]
                if raw_multiplier in chinese_multipliers
                else float(raw_multiplier)
            )
            multiplier = min(3.0, max(0.1, float(multiplier)))
        except (TypeError, ValueError):
            continue
        # "X is 1.5x the others" uses the pursuer baseline as the common
        # reference, which keeps the requested ratio visible in telemetry.
        speed_mps_by_robot[robot_id] = round(pursuer_speed_mps * multiplier, 2)
    if evader in speed_mps_by_robot:
        evader_speed_mps = speed_mps_by_robot[evader]
    return {
        "type": "pursuit",
        "pursuers": pursuers,
        "evader": evader,
        "participants": participants,
        "capture_radius_m": 6.0,
        "max_rounds": max_rounds,
        "max_world_steps": max_rounds * len(participants),
        "decision_interval_s": 0.75,
        "pursuer_speed_mps": pursuer_speed_mps,
        "evader_speed_mps": evader_speed_mps,
        "speed_mps_by_robot": speed_mps_by_robot,
        "communication_range_m": 55.0,
        "sensor_range_m": 85.0,
        "collision_radius_m": 5.0,
        "encirclement_radius_m": 4.5,
        "required_capture_agents": 2,
        "fast_tactical": True,
        "arena_radius_m": 75.0,
    }


def parse_pursuit_spec(value, available_robot_ids) -> dict | None:
    """Validate structured pursuit parameters emitted by the Commander LLM."""
    if not isinstance(value, dict):
        return None
    kind = str(value.get("type") or "").strip().lower()
    if kind not in {"pursuit", "pursuit_evasion", "chase"}:
        return None
    available = {normalize_robot_id(item) for item in available_robot_ids or []}

    def robot_id(raw):
        candidate = normalize_robot_id(raw)
        if candidate in available:
            return candidate
        match = re.fullmatch(r"UAV_?(\d+)", candidate)
        alias = f"UAV_{int(match.group(1))}" if match else ""
        return alias if alias in available else ""

    evader = robot_id(value.get("evader"))
    pursuers = []
    for item in value.get("pursuers") or []:
        candidate = robot_id(item)
        if candidate and candidate != evader and candidate not in pursuers:
            pursuers.append(candidate)
    if not evader or not pursuers:
        return None
    participants = [*pursuers, evader]

    def number(raw, default, minimum, maximum):
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            parsed = float(default)
        return min(float(maximum), max(float(minimum), parsed))

    pursuer_speed = number(value.get("pursuer_speed_mps"), 14.0, 0.1, 30.0)
    evader_speed = number(value.get("evader_speed_mps"), 9.0, 0.1, 30.0)
    speed_by_robot = {}
    ratios = {}
    raw_ratios = value.get("speed_ratio_by_robot") or {}
    if isinstance(raw_ratios, dict):
        for raw_robot, raw_ratio in raw_ratios.items():
            candidate = robot_id(raw_robot)
            if candidate not in participants:
                continue
            ratio = number(raw_ratio, 1.0, 0.1, 3.0)
            ratios[candidate] = round(ratio, 3)
            speed_by_robot[candidate] = round(pursuer_speed * ratio, 2)

    raw_speeds = value.get("speed_mps_by_robot") or {}
    if isinstance(raw_speeds, dict):
        for raw_robot, raw_speed in raw_speeds.items():
            candidate = robot_id(raw_robot)
            if candidate not in participants:
                continue
            speed_by_robot[candidate] = round(number(raw_speed, 0.1, 0.1, 30.0), 2)

    if evader in speed_by_robot:
        evader_speed = speed_by_robot[evader]
    elif "evader_speed_mps" in value:
        speed_by_robot[evader] = round(evader_speed, 2)

    max_rounds = int(round(number(value.get("max_rounds"), 16, 4, 48)))
    decision_interval = number(value.get("decision_interval_s"), 0.75, 0.1, 3.0)
    capture_radius = number(value.get("capture_radius_m"), 6.0, 1.0, 30.0)
    communication_range = number(value.get("communication_range_m"), 55.0, 5.0, 500.0)
    sensor_range = number(value.get("sensor_range_m"), 85.0, 5.0, 500.0)
    collision_radius = number(value.get("collision_radius_m"), 5.0, 1.0, 20.0)
    encirclement_radius = number(value.get("encirclement_radius_m"), 4.5, 1.0, 20.0)
    required_capture_agents = int(round(number(
        value.get("required_capture_agents"),
        min(2, len(pursuers)),
        1,
        len(pursuers),
    )))

    return {
        "type": "pursuit",
        "pursuers": pursuers,
        "evader": evader,
        "participants": participants,
        "capture_radius_m": round(capture_radius, 3),
        "max_rounds": max_rounds,
        "max_world_steps": max_rounds * len(participants),
        "decision_interval_s": round(decision_interval, 3),
        "pursuer_speed_mps": round(pursuer_speed, 2),
        "evader_speed_mps": round(evader_speed, 2),
        "speed_mps_by_robot": speed_by_robot,
        "speed_ratio_by_robot": ratios,
        "communication_range_m": round(communication_range, 2),
        "sensor_range_m": round(sensor_range, 2),
        "collision_radius_m": round(collision_radius, 2),
        "encirclement_radius_m": round(encirclement_radius, 2),
        "required_capture_agents": required_capture_agents,
        "fast_tactical": bool(value.get("fast_tactical", True)),
        "arena_radius_m": number(value.get("arena_radius_m"), 75.0, 10.0, 1000.0),
        "area_bounds": normalize_area_bounds(value.get("area_bounds")),
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


def boundary_status(position, bounds, warning_margin_m=None) -> dict:
    """Return agent-facing hard-boundary state and distances to each edge."""
    area = normalize_area_bounds(bounds)
    if not area:
        return {
            "status": "unbounded",
            "near": False,
            "outside": False,
            "distances_m": {},
            "warning": "",
        }
    point = _vector(position)
    distances = {
        "north_min": round(point[0] - area["north_min"], 3),
        "north_max": round(area["north_max"] - point[0], 3),
        "east_min": round(point[1] - area["east_min"], 3),
        "east_max": round(area["east_max"] - point[1], 3),
    }
    spans = (
        area["north_max"] - area["north_min"],
        area["east_max"] - area["east_min"],
    )
    margin = (
        float(warning_margin_m)
        if warning_margin_m is not None
        else min(10.0, max(3.0, min(spans) * 0.12))
    )
    outside_edges = [edge for edge, distance in distances.items() if distance < 0.0]
    near_edges = [
        edge for edge, distance in distances.items()
        if 0.0 <= distance <= margin
    ]
    status = "outside" if outside_edges else "warning" if near_edges else "safe"
    nearest_edge = min(distances, key=distances.get)
    nearest_distance = round(distances[nearest_edge], 3)
    if status == "outside":
        warning = (
            f"OUTSIDE hard mission boundary at {', '.join(outside_edges)}; "
            "stop outward motion and return inside."
        )
    elif status == "warning":
        warning = (
            f"Boundary warning: {nearest_edge} is only {max(0.0, nearest_distance):.1f} m away; "
            "turn inward before the next move."
        )
    else:
        warning = ""
    return {
        "status": status,
        "near": status == "warning",
        "outside": status == "outside",
        "margin_m": round(margin, 3),
        "nearest_edge": nearest_edge,
        "nearest_distance_m": nearest_distance,
        "distances_m": distances,
        "warning": warning,
        "bounds": area,
    }


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
        "boundary_status": boundary_status(
            own_position,
            spec.get("area_bounds"),
        ),
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
        _agent_speed_mps(observation, spec),
        float(spec.get("decision_interval_s", 0.75)),
        spec.get("area_bounds"),
    )


def _agent_speed_mps(observation: dict, spec: dict) -> float:
    robot_id = str(observation.get("robot_id") or "")
    overrides = spec.get("speed_mps_by_robot") or {}
    if robot_id in overrides:
        try:
            return max(0.0, float(overrides[robot_id]))
        except (TypeError, ValueError):
            pass
    role = str(observation.get("role") or "pursuer")
    return max(
        0.0,
        float(
            spec.get("evader_speed_mps", 6.2)
            if role == "evader"
            else spec.get("pursuer_speed_mps", 10.0)
        ),
    )


def encirclement_slot(observation: dict, spec: dict) -> dict | None:
    """Return this pursuer's moving slot around the predicted evader pose.

    The slot assignment is deterministic from the pursuer ordering in the
    mission spec. Every agent can reproduce its own slot from local sensing,
    so no central step-by-step controller is needed. The slot radius is below
    the capture radius while the angular spacing remains above the collision
    radius for the usual three-pursuer task.
    """
    if str(observation.get("role") or "") != "pursuer":
        return None
    evader_id = str(spec.get("evader") or "")
    observed = dict(observation.get("observed_uavs") or {})
    target = observed.get(evader_id)
    if not isinstance(target, dict):
        return None

    pursuers = [str(item) for item in spec.get("pursuers") or []]
    robot_id = str(observation.get("robot_id") or "")
    try:
        slot_index = pursuers.index(robot_id)
    except ValueError:
        return None
    count = max(1, len(pursuers))

    target_position = _vector(target.get("position"))
    target_velocity = _vector(target.get("velocity"))
    interval = float(spec.get("decision_interval_s", 0.75))
    lead_seconds = min(1.5, max(0.6, interval * 1.5))
    predicted_target = [
        target_position[0] + target_velocity[0] * lead_seconds,
        target_position[1] + target_velocity[1] * lead_seconds,
        target_position[2],
    ]

    velocity_heading = math.atan2(target_velocity[1], target_velocity[0])
    if math.hypot(target_velocity[0], target_velocity[1]) < 0.2:
        velocity_heading = 0.0
    angle = velocity_heading + (2.0 * math.pi * slot_index / count)
    radius = min(
        float(spec.get("encirclement_radius_m", 4.5)),
        max(2.5, float(spec.get("capture_radius_m", 6.0)) * 0.9),
    )
    slot = [
        predicted_target[0] + radius * math.cos(angle),
        predicted_target[1] + radius * math.sin(angle),
        predicted_target[2],
    ]
    return {
        "target_position": target_position,
        "predicted_target": predicted_target,
        "slot": slot,
        "slot_index": slot_index,
        "slot_count": count,
        "slot_angle_deg": round(math.degrees(angle) % 360.0, 1),
        "slot_radius_m": radius,
    }


def encirclement_direction(observation: dict, spec: dict) -> list[float]:
    """Fast local policy: close on a predicted target while taking a ring slot."""
    own = _vector(observation.get("position"))
    slot_data = encirclement_slot(observation, spec)
    if not slot_data:
        return tactical_direction(observation, spec)

    slot = slot_data["slot"]
    desired = [slot[0] - own[0], slot[1] - own[1], 0.0]
    target_velocity = _vector(
        (observation.get("observed_uavs") or {}).get(str(spec.get("evader") or ""), {}).get("velocity")
    )
    # Carry the target's motion into the intercept vector so the ring follows
    # a moving evader instead of repeatedly chasing its old position.
    desired[0] += target_velocity[0] * 0.75
    desired[1] += target_velocity[1] * 0.75

    collision_radius = float(spec.get("collision_radius_m", 5.0))
    safe_distance = collision_radius * 1.55
    for peer_id, item in (observation.get("observed_uavs") or {}).items():
        if peer_id == str(spec.get("evader") or "") or item.get("role") != "pursuer":
            continue
        distance = float(item.get("distance_m") or 0.0)
        if 1e-6 < distance < safe_distance:
            strength = (safe_distance - distance) / safe_distance
            peer_position = _vector(item.get("position"))
            desired[0] += (own[0] - peer_position[0]) / distance * strength * 7.0
            desired[1] += (own[1] - peer_position[1]) / distance * strength * 7.0

    return constrain_direction_to_area(
        own,
        _unit(desired),
        _agent_speed_mps(observation, spec),
        float(spec.get("decision_interval_s", 0.75)),
        spec.get("area_bounds"),
    )


def encirclement_motion_decision(robot_id: str, observation: dict, spec: dict) -> dict:
    """Build an immediate agent action without a blocking LLM round-trip."""
    direction = encirclement_direction(observation, spec)
    speed = _agent_speed_mps(observation, spec)
    slot_data = encirclement_slot(observation, spec)
    area = normalize_area_bounds(observation.get("area_bounds"))
    boundary = observation.get("boundary_status") or boundary_status(
        observation.get("position"),
        area,
    )
    boundary_note = ""
    if area:
        boundary_note = (
            f" Hard boundary N=[{area['north_min']:.1f}, {area['north_max']:.1f}], "
            f"E=[{area['east_min']:.1f}, {area['east_max']:.1f}]; "
            "all initialization and movement must remain inside."
        )
        if boundary.get("warning") or boundary.get("outside"):
            boundary_note += f" WARNING: {boundary.get('warning')}"
    if slot_data:
        message = (
            f"{robot_id} takes encirclement slot {slot_data['slot_index'] + 1}/"
            f"{slot_data['slot_count']} at {slot_data['slot_angle_deg']:.0f} degrees; "
            f"intercepting the predicted target position while keeping "
            f"{slot_data['slot_radius_m']:.1f} m capture radius."
            f"{boundary_note}"
        )
    else:
        message = (
            f"{robot_id} continues local pursuit toward the observed target."
            f"{boundary_note}"
        )
    return {
        "direction": [round(direction[0], 4), round(direction[1], 4), 0.0],
        "speed_mps": round(speed, 3),
        "message": message,
        "request_states": list(observation.get("communicable_peers") or []),
        "source": "fast_encirclement",
    }
def motion_decision_prompt(robot_id: str, observation: dict, spec: dict, previous_direction) -> str:
    """Prompt one UAV for its next local motion decision, not a global plan."""
    return f"""You are {robot_id}, one independent motion agent in a pursuit-evasion mission.
You may reason only from the supplied local observation and messages in your own context.
Choose your own horizontal NED direction. Keep the previous direction when it remains useful;
the vehicle will continue moving without another command until you deliberately turn or the mission ends.
Communicate a concise state/intent update to locally reachable peers.
The mission boundary is a hard physical constraint. If boundary_status is
warning or outside, turn inward immediately and mention the warning in message.
Never propose a direction that moves through the boundary.

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
    if spec.get("fast_tactical") and observation.get("role") == "pursuer":
        tactical = encirclement_direction(observation, spec)
    raw_direction = parsed.get("direction")
    try:
        proposed = _unit([float(raw_direction[0]), float(raw_direction[1]), 0.0])
        tactical_weight = 0.9 if spec.get("fast_tactical") else 0.65
        model_weight = 1.0 - tactical_weight
        combined = _unit([
            tactical[0] * tactical_weight + proposed[0] * model_weight,
            tactical[1] * tactical_weight + proposed[1] * model_weight,
            0.0,
        ])
        source = "llm+tactical"
    except (TypeError, ValueError, IndexError):
        combined = tactical
        source = "tactical_fallback"

    combined = constrain_direction_to_area(
        observation.get("position"),
        combined,
        _agent_speed_mps(observation, spec),
        float(spec.get("decision_interval_s", 0.75)),
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
    boundary = observation.get("boundary_status") or boundary_status(
        observation.get("position"),
        spec.get("area_bounds"),
    )
    if boundary.get("warning") or boundary.get("outside"):
        message = f"{boundary.get('warning')} {message}".strip()
    speed = _agent_speed_mps(observation, spec)
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
    capture_radius = float(spec.get("capture_radius_m", 5.0))
    captured_ids = [robot_id for robot_id, distance in distances.items() if distance <= capture_radius]
    required_capture_agents = min(len(pursuers), max(1, int(spec.get("required_capture_agents", 1))))
    evidence = {
        "closest_pursuer": closest_id,
        "closest_distance_m": round(closest_distance, 2),
        "capture_radius_m": capture_radius,
        "captured_pursuers": captured_ids,
        "captured_count": len(captured_ids),
        "required_capture_agents": required_capture_agents,
        "round_index": int(round_index),
        "world_step": int(world_step),
    }
    if len(captured_ids) >= required_capture_agents:
        return {
            "status": "complete",
            "reason": f"{len(captured_ids)} pursuers formed the capture envelope around {evader}.",
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
    "parse_pursuit_spec",
    "boundary_status",
    "normalize_area_bounds",
    "position_inside_area",
    "pursuit_links",
    "scenario_copy",
    "tactical_direction",
]
