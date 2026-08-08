"""Animated task-level multi-UAV skills for the Mock adapter."""

import math
import time

from skills.base_skill import Skill, SkillResult
from skills.swarm_skills import (
    _polyline_length,
    _robot_sort_key,
    formation_offsets,
    interpolate_polyline,
    minimum_separation,
    normalize_robot_ids,
)


def _coerce_point(raw, default_down=-18.0):
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        raise ValueError("Position must contain at least [N, E]")
    down = float(raw[2]) if len(raw) >= 3 else float(default_down)
    if down >= 0:
        down = -max(5.0, down)
    return (float(raw[0]), float(raw[1]), down)


def _mock_fleet(input_data, minimum=2):
    from adapters.adapter_manager import get_primary_adapter

    adapter = get_primary_adapter()
    if adapter is None:
        raise RuntimeError("No simulation adapter")
    if getattr(adapter, "name", "") != "mock":
        raise RuntimeError("This animated mission skill currently requires Mock mode")
    if not callable(getattr(adapter, "get_robot_snapshot", None)):
        raise RuntimeError("Mock fleet animation API is unavailable")
    if not callable(getattr(adapter, "set_robot_position", None)):
        raise RuntimeError("Mock fleet animation API is unavailable")

    snapshot = adapter.get_robot_snapshot()
    robot_ids = normalize_robot_ids(input_data.get("robot_ids"), adapter)
    unknown = [robot_id for robot_id in robot_ids if robot_id not in snapshot]
    if unknown:
        raise ValueError(f"Unknown mock UAVs: {', '.join(unknown)}")
    if len(robot_ids) < minimum:
        raise ValueError(f"At least {minimum} active UAVs are required")
    if len(robot_ids) > 10:
        raise ValueError("A maximum of 10 UAVs is supported")
    return adapter, snapshot, robot_ids


def _staged_path(initial, entry, layer_down):
    return [
        (float(initial[0]), float(initial[1]), float(layer_down)),
        (float(entry[0]), float(entry[1]), float(layer_down)),
        tuple(float(value) for value in entry[:3]),
    ]


def _freeze_fleet(adapter, robot_ids):
    snapshot = adapter.get_robot_snapshot()
    for robot_id in robot_ids:
        position = snapshot.get(robot_id, {}).get("position")
        if position:
            adapter.set_robot_position(
                robot_id,
                *position,
                velocity=[0.0, 0.0, 0.0],
                moving=False,
                in_air=True,
            )


def animate_mock_paths(
    adapter,
    snapshot,
    paths,
    speed,
    visual_duration=None,
    minimum_separation_m=6.0,
):
    robot_ids = sorted(paths, key=_robot_sort_key)
    full_paths = {}
    for robot_id in robot_ids:
        initial = tuple(float(value) for value in snapshot[robot_id]["position"][:3])
        planned = [tuple(float(value) for value in point[:3]) for point in paths[robot_id]]
        if not planned:
            raise ValueError(f"No route was generated for {robot_id}")
        full_paths[robot_id] = [initial] + planned

    max_distance = max(_polyline_length(path) for path in full_paths.values())
    if visual_duration is None:
        duration = max(4.0, min(max_distance / max(float(speed), 0.1), 14.0))
    else:
        duration = max(0.1, min(float(visual_duration), 30.0))
    frames = max(2, int(math.ceil(duration * 10.0)))
    frame_seconds = duration / frames
    previous = {
        robot_id: tuple(snapshot[robot_id]["position"][:3])
        for robot_id in robot_ids
    }
    minimum_observed = minimum_separation(previous.values())
    emergency_distance = max(2.5, float(minimum_separation_m) * 0.45)

    try:
        for frame in range(frames + 1):
            progress = frame / frames
            current = {
                robot_id: interpolate_polyline(full_paths[robot_id], progress)
                for robot_id in robot_ids
            }
            observed = minimum_separation(current.values())
            minimum_observed = min(minimum_observed, observed)
            if observed < emergency_distance:
                raise RuntimeError(f"Emergency separation breach: {observed:.2f}m")
            for robot_id in robot_ids:
                point = current[robot_id]
                velocity = (
                    [
                        (point[axis] - previous[robot_id][axis]) / frame_seconds
                        for axis in range(3)
                    ]
                    if frame > 0
                    else [0.0, 0.0, 0.0]
                )
                adapter.set_robot_position(
                    robot_id,
                    *point,
                    velocity=velocity,
                    moving=frame < frames,
                    in_air=True,
                )
            previous = current
            if frame < frames:
                time.sleep(frame_seconds)
    except Exception:
        _freeze_fleet(adapter, robot_ids)
        raise

    final_snapshot = adapter.get_robot_snapshot()
    return {
        "duration_s": round(duration, 3),
        "frames": frames,
        "minimum_observed_separation_m": round(minimum_observed, 2),
        "final_positions": {
            robot_id: [
                round(float(value), 2)
                for value in final_snapshot[robot_id]["position"][:3]
            ]
            for robot_id in robot_ids
        },
    }


def build_perimeter_patrol_paths(
    robot_ids,
    center_position,
    area_width,
    area_height,
    patrol_laps=1,
):
    robots = sorted([str(robot_id) for robot_id in robot_ids], key=_robot_sort_key)
    center_n, center_e, center_d = _coerce_point(center_position)
    width = max(float(area_width), 1.0)
    height = max(float(area_height), 1.0)
    sample_count = max(12, len(robots) * 6)
    corners = [
        (center_n - height / 2.0, center_e - width / 2.0, center_d),
        (center_n + height / 2.0, center_e - width / 2.0, center_d),
        (center_n + height / 2.0, center_e + width / 2.0, center_d),
        (center_n - height / 2.0, center_e + width / 2.0, center_d),
    ]
    lengths = (height, width, height, width)
    perimeter = 2.0 * (width + height)
    points = []
    for sample in range(sample_count):
        target = perimeter * sample / sample_count
        traversed = 0.0
        for index, length in enumerate(lengths):
            if target <= traversed + length or index == 3:
                start = corners[index]
                end = corners[(index + 1) % 4]
                ratio = (target - traversed) / max(length, 1e-9)
                points.append(tuple(
                    start[axis] + (end[axis] - start[axis]) * ratio
                    for axis in range(3)
                ))
                break
            traversed += length

    laps = max(1, min(int(patrol_laps), 3))
    paths = {}
    for index, robot_id in enumerate(robots):
        phase = int(round(index * sample_count / len(robots))) % sample_count
        rotated = points[phase:] + points[:phase]
        route = rotated * laps
        paths[robot_id] = route + [rotated[0]]
    return paths


def assign_waypoint_inspection_paths(robot_ids, inspection_points):
    robots = sorted([str(robot_id) for robot_id in robot_ids], key=_robot_sort_key)
    points = [_coerce_point(point) for point in inspection_points]
    if len(points) < len(robots):
        raise ValueError("Provide at least one inspection point per UAV")
    paths = {robot_id: [] for robot_id in robots}
    for index, point in enumerate(points):
        paths[robots[index % len(robots)]].append(point)
    return paths


def build_relay_positions(robot_ids, start_position, end_position):
    robots = sorted([str(robot_id) for robot_id in robot_ids], key=_robot_sort_key)
    start = _coerce_point(start_position)
    end = _coerce_point(end_position, default_down=start[2])
    return {
        robot_id: tuple(
            start[axis] + (end[axis] - start[axis]) * (index + 1) / (len(robots) + 1)
            for axis in range(3)
        )
        for index, robot_id in enumerate(robots)
    }


def build_escort_paths(robot_ids, route, formation="v", spacing=10.0):
    robots = sorted([str(robot_id) for robot_id in robot_ids], key=_robot_sort_key)
    centers = [_coerce_point(point) for point in route]
    if len(centers) < 2:
        raise ValueError("Escort route requires at least two waypoints")
    offsets = formation_offsets(len(robots), formation, spacing)
    return {
        robot_id: [
            (
                center[0] + offsets[index][0],
                center[1] + offsets[index][1],
                center[2],
            )
            for center in centers
        ]
        for index, robot_id in enumerate(robots)
    }


class _MockMissionSkill(Skill):
    skill_type = "hard"
    skill_level = "advanced"
    robot_type = ["UAV"]
    preconditions = []
    terminal_on_success = True
    cost = 7.0

    @staticmethod
    def _failure(started, exc):
        return SkillResult(
            success=False,
            error_msg=str(exc),
            cost_time=round(time.time() - started, 3),
            logs=[f"Mission stopped safely: {exc}"],
        )


class SwarmPerimeterPatrol(_MockMissionSkill):
    name = "swarm_perimeter_patrol"
    description = "Split a rectangular perimeter among multiple UAVs and patrol it in synchronized phases."
    input_schema = {
        "robot_ids": "UAV IDs separated by commas; defaults to every active UAV",
        "area_center": "[N, E, D] patrol-area center",
        "area_width": "east-west width in meters",
        "area_height": "north-south height in meters",
        "altitude": "positive flight altitude when D is omitted",
        "speed": "visual patrol speed in m/s",
        "patrol_laps": "number of perimeter laps, 1 to 3",
        "visual_duration": "optional Mock animation duration",
    }
    output_schema = {
        "patrol_paths": "phase-shifted perimeter route for each UAV",
        "minimum_observed_separation_m": "minimum fleet separation",
        "completion_summary": "English terminal summary",
        "completion_summary_zh": "Chinese terminal summary",
    }

    def execute(self, input_data):
        started = time.time()
        try:
            adapter, snapshot, robot_ids = _mock_fleet(input_data)
            center_raw = input_data.get("area_center") or [30.0, 30.0]
            altitude = max(5.0, min(float(input_data.get("altitude", 15.0)), 60.0))
            center = _coerce_point(center_raw, default_down=-altitude)
            width = max(30.0, min(float(input_data.get("area_width", 100.0)), 300.0))
            height = max(30.0, min(float(input_data.get("area_height", 80.0)), 300.0))
            speed = max(2.0, min(float(input_data.get("speed", 20.0)), 40.0))
            laps = max(1, min(int(input_data.get("patrol_laps", 1)), 3))
            mission_paths = build_perimeter_patrol_paths(
                robot_ids, center, width, height, laps
            )
            transit_base = min(
                [center[2]]
                + [float(snapshot[robot_id]["position"][2]) for robot_id in robot_ids]
            ) - 8.0
            paths = {}
            for index, robot_id in enumerate(robot_ids):
                entry = mission_paths[robot_id][0]
                paths[robot_id] = (
                    _staged_path(
                        snapshot[robot_id]["position"],
                        entry,
                        transit_base - index * 4.0,
                    )
                    + mission_paths[robot_id][1:]
                )
            animation = animate_mock_paths(
                adapter,
                snapshot,
                paths,
                speed,
                input_data.get("visual_duration"),
                minimum_separation_m=6.0,
            )
            perimeter = 2.0 * (width + height)
            return SkillResult(
                success=True,
                output={
                    "robots": robot_ids,
                    "area_center": [round(value, 2) for value in center],
                    "area_width_m": round(width, 2),
                    "area_height_m": round(height, 2),
                    "patrol_laps": laps,
                    "perimeter_distance_m": round(perimeter * laps, 2),
                    "patrol_paths": {
                        robot_id: [[round(value, 2) for value in point] for point in route]
                        for robot_id, route in mission_paths.items()
                    },
                    "completion_summary": (
                        f"Perimeter patrol complete: {len(robot_ids)} UAVs "
                        f"covered {perimeter * laps:.0f} meters."
                    ),
                    "completion_summary_zh": (
                        f"边界巡查完成：{len(robot_ids)} 架无人机协同巡查 "
                        f"{perimeter * laps:.0f} 米。"
                    ),
                    **animation,
                },
                cost_time=round(time.time() - started, 3),
                logs=[f"Perimeter patrol complete with {len(robot_ids)} UAVs"],
            )
        except Exception as exc:
            return self._failure(started, exc)


class SwarmWaypointInspection(_MockMissionSkill):
    name = "swarm_waypoint_inspection"
    description = "Assign inspection waypoints across multiple UAVs and visit them concurrently at separated altitude layers."
    input_schema = {
        "robot_ids": "UAV IDs separated by commas; defaults to every active UAV",
        "inspection_points": "list of [N, E, D] inspection points",
        "altitude": "positive default inspection altitude",
        "speed": "visual inspection speed in m/s",
        "visual_duration": "optional Mock animation duration",
    }
    output_schema = {
        "assignments": "inspection waypoints assigned to each UAV",
        "points_inspected": "total completed inspection points",
        "minimum_observed_separation_m": "minimum fleet separation",
        "completion_summary": "English terminal summary",
        "completion_summary_zh": "Chinese terminal summary",
    }

    def execute(self, input_data):
        started = time.time()
        try:
            adapter, snapshot, robot_ids = _mock_fleet(input_data)
            assignments = assign_waypoint_inspection_paths(
                robot_ids, input_data.get("inspection_points") or []
            )
            altitude = max(5.0, min(float(input_data.get("altitude", 18.0)), 60.0))
            speed = max(2.0, min(float(input_data.get("speed", 18.0)), 40.0))
            base_down = min(
                [-altitude]
                + [point[2] for route in assignments.values() for point in route]
            )
            paths = {}
            mission_paths = {}
            for index, robot_id in enumerate(robot_ids):
                layer_down = base_down - index * 4.0
                layered = [
                    (point[0], point[1], layer_down)
                    for point in assignments[robot_id]
                ]
                mission_paths[robot_id] = layered
                paths[robot_id] = (
                    _staged_path(
                        snapshot[robot_id]["position"],
                        layered[0],
                        layer_down,
                    )
                    + layered[1:]
                )
            animation = animate_mock_paths(
                adapter,
                snapshot,
                paths,
                speed,
                input_data.get("visual_duration"),
                minimum_separation_m=6.0,
            )
            point_count = sum(len(route) for route in mission_paths.values())
            return SkillResult(
                success=True,
                output={
                    "robots": robot_ids,
                    "points_inspected": point_count,
                    "assignments": {
                        robot_id: [[round(value, 2) for value in point] for point in route]
                        for robot_id, route in mission_paths.items()
                    },
                    "completion_summary": (
                        f"Waypoint inspection complete: {len(robot_ids)} UAVs "
                        f"visited {point_count} points."
                    ),
                    "completion_summary_zh": (
                        f"多点巡检完成：{len(robot_ids)} 架无人机已检查 "
                        f"{point_count} 个任务点。"
                    ),
                    **animation,
                },
                cost_time=round(time.time() - started, 3),
                logs=[f"Waypoint inspection complete: {point_count} points"],
            )
        except Exception as exc:
            return self._failure(started, exc)


class SwarmRelayDeploy(_MockMissionSkill):
    name = "swarm_relay_deploy"
    description = "Deploy multiple UAVs as an evenly spaced airborne relay chain between two endpoints."
    input_schema = {
        "robot_ids": "UAV IDs separated by commas; defaults to every active UAV",
        "start_position": "[N, E, D] first communication endpoint",
        "end_position": "[N, E, D] second communication endpoint",
        "min_spacing": "required minimum relay spacing in meters",
        "speed": "visual deployment speed in m/s",
        "visual_duration": "optional Mock animation duration",
    }
    output_schema = {
        "relay_positions": "final ordered relay positions",
        "minimum_observed_separation_m": "minimum fleet separation",
        "completion_summary": "English terminal summary",
        "completion_summary_zh": "Chinese terminal summary",
    }

    def execute(self, input_data):
        started = time.time()
        try:
            adapter, snapshot, robot_ids = _mock_fleet(input_data)
            start = _coerce_point(input_data.get("start_position") or [0.0, 0.0, -18.0])
            end = _coerce_point(input_data.get("end_position") or [100.0, 60.0, -18.0])
            min_spacing = max(6.0, min(float(input_data.get("min_spacing", 10.0)), 50.0))
            speed = max(2.0, min(float(input_data.get("speed", 18.0)), 40.0))
            relay_positions = build_relay_positions(robot_ids, start, end)
            planned_min = minimum_separation(relay_positions.values())
            if planned_min < min_spacing:
                raise ValueError(
                    f"Relay segment is too short: planned spacing {planned_min:.2f}m "
                    f"is below {min_spacing:.2f}m"
                )
            transit_base = min(
                [start[2], end[2]]
                + [float(snapshot[robot_id]["position"][2]) for robot_id in robot_ids]
            ) - 8.0
            paths = {
                robot_id: _staged_path(
                    snapshot[robot_id]["position"],
                    relay_positions[robot_id],
                    transit_base - index * 4.0,
                )
                for index, robot_id in enumerate(robot_ids)
            }
            animation = animate_mock_paths(
                adapter,
                snapshot,
                paths,
                speed,
                input_data.get("visual_duration"),
                minimum_separation_m=min_spacing,
            )
            return SkillResult(
                success=True,
                output={
                    "robots": robot_ids,
                    "start_position": [round(value, 2) for value in start],
                    "end_position": [round(value, 2) for value in end],
                    "relay_spacing_m": round(planned_min, 2),
                    "relay_positions": {
                        robot_id: [round(value, 2) for value in point]
                        for robot_id, point in relay_positions.items()
                    },
                    "completion_summary": (
                        f"Relay deployment complete: {len(robot_ids)} UAVs "
                        f"formed a chain with {planned_min:.1f} meter spacing."
                    ),
                    "completion_summary_zh": (
                        f"空中中继部署完成：{len(robot_ids)} 架无人机已形成 "
                        f"平均间距 {planned_min:.1f} 米的通信链路。"
                    ),
                    **animation,
                },
                cost_time=round(time.time() - started, 3),
                logs=[f"Relay chain deployed with {len(robot_ids)} UAVs"],
            )
        except Exception as exc:
            return self._failure(started, exc)


class SwarmEscortRoute(_MockMissionSkill):
    name = "swarm_escort_route"
    description = "Escort along a waypoint route while preserving a collision-safe multi-UAV formation."
    input_schema = {
        "robot_ids": "UAV IDs separated by commas; defaults to every active UAV",
        "route": "ordered centerline waypoints as [[N, E, D], ...]",
        "formation": "triangle | circle | line | v",
        "spacing": "minimum formation spacing in meters",
        "speed": "visual escort speed in m/s",
        "visual_duration": "optional Mock animation duration",
    }
    output_schema = {
        "escort_paths": "formation-preserving route for each UAV",
        "minimum_observed_separation_m": "minimum fleet separation",
        "completion_summary": "English terminal summary",
        "completion_summary_zh": "Chinese terminal summary",
    }

    def execute(self, input_data):
        started = time.time()
        try:
            adapter, snapshot, robot_ids = _mock_fleet(input_data)
            route = input_data.get("route") or []
            formation = str(input_data.get("formation") or "v").lower()
            spacing = max(6.0, min(float(input_data.get("spacing", 10.0)), 50.0))
            speed = max(2.0, min(float(input_data.get("speed", 18.0)), 40.0))
            mission_paths = build_escort_paths(robot_ids, route, formation, spacing)
            planned_min = minimum_separation(
                [mission_paths[robot_id][0] for robot_id in robot_ids]
            )
            if planned_min < spacing - 0.05:
                raise RuntimeError(
                    f"Escort formation spacing is unsafe: {planned_min:.2f}m"
                )
            first_down = min(path[0][2] for path in mission_paths.values())
            transit_base = min(
                [first_down]
                + [float(snapshot[robot_id]["position"][2]) for robot_id in robot_ids]
            ) - 8.0
            paths = {}
            for index, robot_id in enumerate(robot_ids):
                entry = mission_paths[robot_id][0]
                paths[robot_id] = (
                    _staged_path(
                        snapshot[robot_id]["position"],
                        entry,
                        transit_base - index * 4.0,
                    )
                    + mission_paths[robot_id][1:]
                )
            animation = animate_mock_paths(
                adapter,
                snapshot,
                paths,
                speed,
                input_data.get("visual_duration"),
                minimum_separation_m=spacing,
            )
            route_distance = _polyline_length([_coerce_point(point) for point in route])
            return SkillResult(
                success=True,
                output={
                    "robots": robot_ids,
                    "formation": formation,
                    "slot_spacing_m": round(planned_min, 2),
                    "route_distance_m": round(route_distance, 2),
                    "escort_paths": {
                        robot_id: [[round(value, 2) for value in point] for point in path]
                        for robot_id, path in mission_paths.items()
                    },
                    "completion_summary": (
                        f"Escort complete: {len(robot_ids)} UAVs maintained a "
                        f"{formation} formation for {route_distance:.0f} meters."
                    ),
                    "completion_summary_zh": (
                        f"编队护航完成：{len(robot_ids)} 架无人机以 {formation} "
                        f"编队飞行 {route_distance:.0f} 米。"
                    ),
                    **animation,
                },
                cost_time=round(time.time() - started, 3),
                logs=[f"Escort route complete with {len(robot_ids)} UAVs"],
            )
        except Exception as exc:
            return self._failure(started, exc)
