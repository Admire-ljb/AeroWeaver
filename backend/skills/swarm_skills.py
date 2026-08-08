"""Collision-aware multi-UAV rendezvous and formation skills."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import math
import re
import time

from adapters.sim_adapter import Position
from skills.base_skill import Skill, SkillResult


def _robot_sort_key(robot_id: str):
    match = re.search(r"(\d+)$", str(robot_id or ""))
    return (int(match.group(1)) if match else 10**9, str(robot_id or ""))


def normalize_robot_ids(raw, primary_adapter=None) -> list[str]:
    if isinstance(raw, str):
        values = re.split(r"[\s,;]+", raw.strip())
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    robots = []
    for value in values:
        robot_id = str(value or "").strip().replace("-", "_")
        if robot_id and robot_id not in robots:
            robots.append(robot_id)

    if not robots and primary_adapter is not None:
        active = getattr(primary_adapter, "_pool_active_robots", None) or []
        robots = [str(item) for item in active if str(item).strip()]
        if not robots:
            get_snapshot = getattr(primary_adapter, "get_robot_snapshot", None)
            snapshot = get_snapshot() if callable(get_snapshot) else {}
            robots = [str(item) for item in snapshot if str(item).strip()]
    return sorted(robots, key=_robot_sort_key)


def formation_offsets(count: int, formation: str, spacing: float) -> list[tuple[float, float]]:
    """Return centered N/E offsets with at least the requested slot spacing."""
    if count < 1:
        return []
    spacing = max(float(spacing), 4.0)
    kind = str(formation or "triangle").strip().lower()

    if count == 1:
        raw = [(0.0, 0.0)]
    elif kind == "line":
        raw = [(0.0, (index - (count - 1) / 2.0) * spacing) for index in range(count)]
    elif kind in {"v", "vee"}:
        raw = [(spacing, 0.0)]
        for index in range(1, count):
            rank = (index + 1) // 2
            side = -1.0 if index % 2 else 1.0
            raw.append((spacing - rank * spacing, side * rank * spacing))
    elif kind in {"triangle", "triangular", "wedge"}:
        row_height = spacing * math.sqrt(3.0) / 2.0
        raw = []
        row = 0
        while len(raw) < count:
            row_count = min(row + 1, count - len(raw))
            for column in range(row_count):
                raw.append(
                    (
                        -row * row_height,
                        (column - row / 2.0) * spacing,
                    )
                )
            row += 1
    elif kind in {"circle", "ring"}:
        radius = spacing / (2.0 * math.sin(math.pi / count))
        raw = [
            (
                radius * math.cos(math.pi / 2.0 + 2.0 * math.pi * index / count),
                radius * math.sin(math.pi / 2.0 + 2.0 * math.pi * index / count),
            )
            for index in range(count)
        ]
    else:
        raise ValueError(f"Unsupported formation: {formation}")

    mean_n = sum(item[0] for item in raw) / count
    mean_e = sum(item[1] for item in raw) / count
    return [(item[0] - mean_n, item[1] - mean_e) for item in raw]


def minimum_separation(positions) -> float:
    points = [tuple(float(value) for value in point[:3]) for point in positions if point is not None]
    if len(points) < 2:
        return float("inf")
    return min(
        math.dist(points[left], points[right])
        for left in range(len(points))
        for right in range(left + 1, len(points))
    )


def build_area_search_paths(
    robot_ids,
    center_position,
    area_width,
    area_height,
    tracks_per_uav=4,
):
    """Divide a rectangle into parallel lawnmower-search strips."""
    robots = sorted([str(robot_id) for robot_id in robot_ids], key=_robot_sort_key)
    if not robots:
        return {}
    center_n, center_e, center_d = [float(value) for value in center_position[:3]]
    width = max(float(area_width), 1.0)
    height = max(float(area_height), 1.0)
    tracks = max(2, int(tracks_per_uav))
    cell_width = width / len(robots)
    south = center_n - height / 2.0
    north = center_n + height / 2.0
    paths = {}
    for robot_index, robot_id in enumerate(robots):
        cell_left = center_e - width / 2.0 + robot_index * cell_width
        waypoints = []
        for track_index in range(tracks):
            lane_e = cell_left + cell_width * (track_index + 0.5) / tracks
            start_n, end_n = (south, north) if track_index % 2 == 0 else (north, south)
            waypoints.append((start_n, lane_e, center_d))
            waypoints.append((end_n, lane_e, center_d))
        paths[robot_id] = waypoints
    return paths


def build_formation_search_paths(
    robot_ids,
    center_position,
    area_width,
    area_height,
    tracks_per_uav=4,
    formation="triangle",
    spacing=12.0,
):
    """Translate one bounded lawnmower route while preserving formation offsets."""
    robots = sorted([str(robot_id) for robot_id in robot_ids], key=_robot_sort_key)
    if not robots:
        return {}, [], []

    center_n, center_e, center_d = [float(value) for value in center_position[:3]]
    width = max(float(area_width), 1.0)
    height = max(float(area_height), 1.0)
    tracks = max(2, int(tracks_per_uav))
    offsets = formation_offsets(len(robots), formation, spacing)

    min_offset_n = min(offset[0] for offset in offsets)
    max_offset_n = max(offset[0] for offset in offsets)
    min_offset_e = min(offset[1] for offset in offsets)
    max_offset_e = max(offset[1] for offset in offsets)
    south = center_n - height / 2.0 - min_offset_n
    north = center_n + height / 2.0 - max_offset_n
    west = center_e - width / 2.0 - min_offset_e
    east = center_e + width / 2.0 - max_offset_e
    if south > north or west > east:
        footprint_width = max_offset_e - min_offset_e
        footprint_height = max_offset_n - min_offset_n
        raise ValueError(
            "Search area is too small for the requested formation: "
            f"need at least {footprint_width:.1f}m x {footprint_height:.1f}m"
        )

    center_path = []
    for track_index in range(tracks):
        ratio = track_index / (tracks - 1)
        lane_e = west + (east - west) * ratio
        start_n, end_n = (south, north) if track_index % 2 == 0 else (north, south)
        center_path.append((start_n, lane_e, center_d))
        center_path.append((end_n, lane_e, center_d))

    paths = {
        robot_id: [
            (point[0] + offsets[index][0], point[1] + offsets[index][1], point[2])
            for point in center_path
        ]
        for index, robot_id in enumerate(robots)
    }
    return paths, center_path, offsets


def interpolate_polyline(points, progress):
    """Interpolate one point at normalized cumulative distance along a path."""
    points = [tuple(float(value) for value in point[:3]) for point in points]
    if not points:
        return (0.0, 0.0, 0.0)
    if len(points) == 1:
        return points[0]
    progress = max(0.0, min(float(progress), 1.0))
    lengths = [math.dist(points[index], points[index + 1]) for index in range(len(points) - 1)]
    total = sum(lengths)
    if total <= 1e-9:
        return points[-1]
    target = progress * total
    traversed = 0.0
    for index, length in enumerate(lengths):
        if target <= traversed + length or index == len(lengths) - 1:
            ratio = 0.0 if length <= 1e-9 else (target - traversed) / length
            return tuple(
                points[index][axis] + (points[index + 1][axis] - points[index][axis]) * ratio
                for axis in range(3)
            )
        traversed += length
    return points[-1]


def _polyline_length(points):
    return sum(math.dist(points[index], points[index + 1]) for index in range(len(points) - 1))


def _position_tuple(position):
    if position is None:
        return None
    return (float(position.north), float(position.east), float(position.down))


class SwarmMotionCoordinator:
    def __init__(self, robot_ids: list[str], min_separation_m: float):
        from adapters.adapter_manager import get_primary_adapter, get_robot_adapter

        self.robot_ids = list(robot_ids)
        self.min_separation_m = max(float(min_separation_m), 4.0)
        self.emergency_separation_m = max(2.5, self.min_separation_m * 0.45)
        self.primary = get_primary_adapter()
        if self.primary is None:
            raise RuntimeError("No simulation adapter")

        parallel_init = getattr(self.primary, "name", "") == "airsim_openfly"
        if parallel_init:
            with ThreadPoolExecutor(max_workers=len(self.robot_ids)) as pool:
                futures = {
                    robot_id: pool.submit(get_robot_adapter, robot_id)
                    for robot_id in self.robot_ids
                }
                self.adapters = {
                    robot_id: future.result()
                    for robot_id, future in futures.items()
                }
        else:
            self.adapters = {
                robot_id: get_robot_adapter(robot_id)
                for robot_id in self.robot_ids
            }
        if any(adapter is None for adapter in self.adapters.values()):
            raise RuntimeError("Unable to create a control channel for every UAV")
        self.parallel = len({id(adapter) for adapter in self.adapters.values()}) == len(self.adapters)
        self.minimum_observed_m = float("inf")

    def read_positions(self) -> dict[str, tuple[float, float, float]]:
        positions = {}
        for robot_id, adapter in self.adapters.items():
            try:
                if not self.parallel:
                    set_active = getattr(adapter, "set_active_robot", None)
                    if callable(set_active):
                        set_active(robot_id)
                get_motion_position = getattr(adapter, "get_motion_position", None)
                position = (
                    get_motion_position()
                    if callable(get_motion_position)
                    else adapter.get_position()
                )
                positions[robot_id] = _position_tuple(position)
            except Exception:
                positions[robot_id] = None
        return positions

    def _stop_all(self):
        for adapter in self.adapters.values():
            request_stop = getattr(adapter, "request_stop", None)
            if callable(request_stop):
                request_stop()

    @staticmethod
    def _move(adapter, target, speed, formation_segment):
        if formation_segment:
            move_segment = getattr(adapter, "fly_formation_segment", None)
            if callable(move_segment):
                return move_segment(target[0], target[1], target[2], speed)
        fly_to_ned = getattr(adapter, "fly_to_ned", None)
        if callable(fly_to_ned):
            return fly_to_ned(target[0], target[1], target[2], speed)
        return adapter.fly_to(
            Position(north=target[0], east=target[1], down=target[2]),
            speed,
        )

    def move_stage(self, targets: dict, speed: float, formation_segment: bool = False):
        if not self.parallel:
            results = {}
            for robot_id in self.robot_ids:
                adapter = self.adapters[robot_id]
                set_active = getattr(adapter, "set_active_robot", None)
                if callable(set_active):
                    set_active(robot_id)
                results[robot_id] = self._move(
                    adapter,
                    targets[robot_id],
                    speed,
                    formation_segment,
                )
                if not results[robot_id].success:
                    return False, results, results[robot_id].message
            return True, results, ""

        results = {}
        with ThreadPoolExecutor(max_workers=len(self.robot_ids)) as pool:
            pending = {
                pool.submit(
                    self._move,
                    self.adapters[robot_id],
                    targets[robot_id],
                    speed,
                    formation_segment,
                ): robot_id
                for robot_id in self.robot_ids
            }
            while pending:
                done, remaining = wait(
                    pending,
                    timeout=0.12,
                    return_when=FIRST_COMPLETED,
                )
                positions = self.read_positions()
                observed = minimum_separation(positions.values())
                self.minimum_observed_m = min(self.minimum_observed_m, observed)
                if observed < self.emergency_separation_m:
                    self._stop_all()
                    for future, robot_id in pending.items():
                        if future in done:
                            results[robot_id] = future.result()
                    wait(pending, timeout=5.0)
                    return (
                        False,
                        results,
                        f"Emergency separation breach: {observed:.2f}m",
                    )
                for future in done:
                    robot_id = pending[future]
                    results[robot_id] = future.result()
                pending = {future: pending[future] for future in remaining}

        failed = {
            robot_id: result.message
            for robot_id, result in results.items()
            if not result.success
        }
        if failed:
            return False, results, "; ".join(
                f"{robot_id}: {message}" for robot_id, message in failed.items()
            )
        return True, results, ""

    def hover(self, duration: float):
        duration = max(float(duration), 0.0)
        if duration <= 0:
            return True, {}, ""
        targets = self.read_positions()
        if not self.parallel:
            results = {}
            for robot_id in self.robot_ids:
                results[robot_id] = self.adapters[robot_id].hover(duration)
            return True, results, ""
        with ThreadPoolExecutor(max_workers=len(self.robot_ids)) as pool:
            futures = {
                robot_id: pool.submit(self.adapters[robot_id].hover, duration)
                for robot_id in self.robot_ids
            }
            results = {
                robot_id: future.result()
                for robot_id, future in futures.items()
            }
        observed = minimum_separation(self.read_positions().values())
        self.minimum_observed_m = min(self.minimum_observed_m, observed)
        failed = [robot_id for robot_id, result in results.items() if not result.success]
        return not failed, results, ", ".join(failed)

    def level_altitudes(self, requested_down: float, speed: float):
        """Raise every UAV onto one terrain-safe NED flight plane."""
        common_down = float(requested_down)
        for _ in range(3):
            positions = self.read_positions()
            if any(position is None for position in positions.values()):
                return False, common_down, "Unable to read every UAV position"
            common_down = min(
                [common_down] + [position[2] for position in positions.values()]
            )
            spread = max(position[2] for position in positions.values()) - min(
                position[2] for position in positions.values()
            )
            if spread <= 0.25 and all(
                abs(position[2] - common_down) <= 0.25
                for position in positions.values()
            ):
                return True, common_down, ""
            targets = {
                robot_id: (position[0], position[1], common_down)
                for robot_id, position in positions.items()
            }
            ok, _, error = self.move_stage(
                targets,
                speed,
                formation_segment=True,
            )
            if not ok:
                return False, common_down, error

        final_positions = self.read_positions()
        final_down = [position[2] for position in final_positions.values()]
        spread = max(final_down) - min(final_down)
        if spread > 0.35:
            return (
                False,
                min(final_down),
                f"Unable to level formation altitude: spread={spread:.2f}m",
            )
        return True, min(final_down), ""


def execute_swarm_motion(input_data: dict, default_formation: str, default_post_action: str) -> SkillResult:
    started = time.time()
    from adapters.adapter_manager import get_primary_adapter

    primary = get_primary_adapter()
    robot_ids = normalize_robot_ids(input_data.get("robot_ids"), primary)
    if len(robot_ids) < 2:
        return SkillResult(
            success=False,
            error_msg="At least two active UAVs are required",
        )
    if len(robot_ids) > 10:
        return SkillResult(success=False, error_msg="A maximum of 10 UAVs is supported")

    spacing = max(min(float(input_data.get("spacing", 10.0)), 50.0), 6.0)
    speed = max(min(float(input_data.get("speed", 6.0)), 10.0), 1.0)
    formation = str(input_data.get("formation") or default_formation).lower()
    post_action = str(input_data.get("post_action") or default_post_action).lower()

    try:
        coordinator = SwarmMotionCoordinator(robot_ids, spacing)
        initial = coordinator.read_positions()
        if any(position is None for position in initial.values()):
            raise RuntimeError("Unable to read every UAV position")

        initial_min = minimum_separation(initial.values())
        if initial_min < coordinator.emergency_separation_m:
            raise RuntimeError(
                f"Unsafe initial geometry: minimum separation is {initial_min:.2f}m"
            )

        center_raw = input_data.get("center_position")
        if isinstance(center_raw, (list, tuple)) and len(center_raw) >= 2:
            center_n = float(center_raw[0])
            center_e = float(center_raw[1])
            center_d = (
                float(center_raw[2])
                if len(center_raw) >= 3
                else sum(position[2] for position in initial.values()) / len(initial)
            )
        else:
            center_n = sum(position[0] for position in initial.values()) / len(initial)
            center_e = sum(position[1] for position in initial.values()) / len(initial)
            center_d = sum(position[2] for position in initial.values()) / len(initial)

        offsets = formation_offsets(len(robot_ids), formation, spacing)
        slots = {
            robot_id: (
                center_n + offsets[index][0],
                center_e + offsets[index][1],
                center_d,
            )
            for index, robot_id in enumerate(robot_ids)
        }
        planned_min = minimum_separation(slots.values())
        if planned_min < spacing - 0.05:
            raise RuntimeError(
                f"Formation planner produced unsafe spacing: {planned_min:.2f}m"
            )

        vertical_gap = max(4.0, spacing * 0.55)
        transit_base = min(
            min(position[2] for position in initial.values()),
            center_d,
        ) - max(8.0, spacing)
        transit_base = max(
            transit_base,
            -110.0 + (len(robot_ids) - 1) * vertical_gap,
        )
        transit_targets = {
            robot_id: (
                initial[robot_id][0],
                initial[robot_id][1],
                transit_base - index * vertical_gap,
            )
            for index, robot_id in enumerate(robot_ids)
        }
        ok, _, error = coordinator.move_stage(transit_targets, speed)
        if not ok:
            raise RuntimeError(f"Altitude-layer entry failed: {error}")

        lane_targets = {
            robot_id: (
                slots[robot_id][0],
                slots[robot_id][1],
                transit_targets[robot_id][2],
            )
            for robot_id in robot_ids
        }
        ok, _, error = coordinator.move_stage(
            lane_targets,
            speed,
            formation_segment=True,
        )
        if not ok:
            raise RuntimeError(f"Staged horizontal approach failed: {error}")

        ok, _, error = coordinator.move_stage(
            slots,
            speed,
            formation_segment=True,
        )
        if not ok:
            raise RuntimeError(f"Formation convergence failed: {error}")

        ok, center_d, error = coordinator.level_altitudes(center_d, speed)
        if not ok:
            raise RuntimeError(f"Formation altitude leveling failed: {error}")
        slots = {
            robot_id: (slot[0], slot[1], center_d)
            for robot_id, slot in slots.items()
        }

        orbit_steps = 0
        if post_action in {"orbit", "rotate", "rotating"}:
            duration = max(min(float(input_data.get("duration", 12.0)), 120.0), 2.0)
            angular_speed = max(
                min(float(input_data.get("angular_speed", 12.0)), 30.0),
                2.0,
            )
            orbit_steps = max(4, min(30, int(math.ceil(duration))))
            max_radius = max(math.hypot(offset[0], offset[1]) for offset in offsets)
            orbit_speed = min(
                10.0,
                max(speed, max_radius * math.radians(angular_speed) + 0.5),
            )
            for step in range(1, orbit_steps + 1):
                angle = math.radians(angular_speed * duration * step / orbit_steps)
                rotated_targets = {}
                for index, robot_id in enumerate(robot_ids):
                    offset_n, offset_e = offsets[index]
                    rotated_targets[robot_id] = (
                        center_n + offset_n * math.cos(angle) - offset_e * math.sin(angle),
                        center_e + offset_n * math.sin(angle) + offset_e * math.cos(angle),
                        center_d,
                    )
                ok, _, error = coordinator.move_stage(
                    rotated_targets,
                    orbit_speed,
                    formation_segment=True,
                )
                if not ok:
                    raise RuntimeError(f"Rotating hold failed at step {step}: {error}")
                actual_positions = coordinator.read_positions()
                center_d = min(
                    [center_d]
                    + [
                        position[2]
                        for position in actual_positions.values()
                        if position is not None
                    ]
                )
                slots = rotated_targets
        else:
            hold_duration = max(
                min(float(input_data.get("hold_duration", 0.0)), 300.0),
                0.0,
            )
            ok, _, error = coordinator.hover(hold_duration)
            if not ok:
                raise RuntimeError(f"Formation hold failed: {error}")

        ok, center_d, error = coordinator.level_altitudes(center_d, speed)
        if not ok:
            raise RuntimeError(f"Final altitude leveling failed: {error}")
        slots = {
            robot_id: (slot[0], slot[1], center_d)
            for robot_id, slot in slots.items()
        }
        final_positions = coordinator.read_positions()
        final_min = minimum_separation(final_positions.values())
        if final_min < coordinator.emergency_separation_m:
            coordinator._stop_all()
            raise RuntimeError(
                f"Final separation verification failed: {final_min:.2f}m"
            )
        slot_errors = {
            robot_id: math.dist(final_positions[robot_id], slots[robot_id])
            for robot_id in robot_ids
        }
        max_slot_error = max(slot_errors.values())
        if max_slot_error > 0.75:
            raise RuntimeError(
                f"Final formation verification failed: slot error {max_slot_error:.2f}m"
            )
        observed = min(
            coordinator.minimum_observed_m,
            initial_min,
            final_min,
        )
        return SkillResult(
            success=True,
            output={
                "robots": robot_ids,
                "center_position": [
                    round(center_n, 2),
                    round(center_e, 2),
                    round(center_d, 2),
                ],
                "formation": formation,
                "post_action": post_action,
                "slot_spacing_m": round(planned_min, 2),
                "minimum_observed_separation_m": round(observed, 2),
                "max_slot_error_m": round(max_slot_error, 2),
                "altitude_spread_m": round(
                    max(position[2] for position in final_positions.values())
                    - min(position[2] for position in final_positions.values()),
                    2,
                ),
                "final_positions": {
                    robot_id: [round(value, 2) for value in position]
                    for robot_id, position in final_positions.items()
                },
                "orbit_steps": orbit_steps,
                "safety_strategy": "altitude_layers_then_slots",
            },
            cost_time=round(time.time() - started, 3),
            logs=[
                (
                    f"Swarm {formation} complete: {len(robot_ids)} UAVs, "
                    f"minimum observed separation {observed:.2f}m"
                )
            ],
        )
    except Exception as exc:
        return SkillResult(
            success=False,
            error_msg=str(exc),
            cost_time=round(time.time() - started, 3),
            logs=[f"Swarm motion stopped safely: {exc}"],
        )


class SwarmAreaSearch(Skill):
    name = "swarm_area_search"
    description = (
        "Cover a rectangular area with independent tracks or preserve an explicitly "
        "requested triangle, circle, line, or V formation in Mock mode."
    )
    skill_type = "hard"
    robot_type = ["UAV"]
    preconditions = []
    cost = 6.0
    terminal_on_success = True
    input_schema = {
        "robot_ids": "UAV IDs separated by commas; defaults to every active UAV",
        "area_center": "[N, E, D] search-area center; D is optional",
        "area_width": "search-area east-west width in meters",
        "area_height": "search-area north-south height in meters",
        "altitude": "positive mock flight altitude in meters when D is omitted",
        "speed": "visual search speed in m/s",
        "tracks_per_uav": "number of parallel scan passes",
        "formation": "coverage | triangle | circle | line | v",
        "formation_spacing": "minimum formation slot spacing in meters",
    }
    output_schema = {
        "search_paths": "per-UAV search waypoints",
        "coverage_percent": "simulated rectangular area coverage",
        "formation": "executed coverage or rigid-formation mode",
        "formation_preserved": "whether requested offsets were preserved during search",
        "completion_summary": "English terminal mission summary",
        "completion_summary_zh": "Chinese terminal mission summary",
        "final_positions": "per-UAV final positions",
    }

    def execute(self, input_data: dict) -> SkillResult:
        started = time.time()
        from adapters.adapter_manager import get_primary_adapter

        adapter = get_primary_adapter()
        if adapter is None:
            return SkillResult(success=False, error_msg="No simulation adapter")
        if getattr(adapter, "name", "") != "mock":
            return SkillResult(
                success=False,
                error_msg="swarm_area_search is currently a Mock-mode visualization skill",
            )
        get_snapshot = getattr(adapter, "get_robot_snapshot", None)
        set_position = getattr(adapter, "set_robot_position", None)
        if not callable(get_snapshot) or not callable(set_position):
            return SkillResult(success=False, error_msg="Mock fleet animation API is unavailable")

        snapshot = get_snapshot()
        robot_ids = normalize_robot_ids(input_data.get("robot_ids"), adapter)
        unknown = [robot_id for robot_id in robot_ids if robot_id not in snapshot]
        if unknown:
            return SkillResult(success=False, error_msg=f"Unknown mock UAVs: {', '.join(unknown)}")
        if len(robot_ids) < 2:
            return SkillResult(success=False, error_msg="At least two active UAVs are required")
        if len(robot_ids) > 10:
            return SkillResult(success=False, error_msg="A maximum of 10 UAVs is supported")

        try:
            center_raw = input_data.get("area_center") or input_data.get("center_position") or [30.0, 30.0]
            if not isinstance(center_raw, (list, tuple)) or len(center_raw) < 2:
                raise ValueError("area_center must contain at least [N, E]")
            altitude = max(5.0, min(float(input_data.get("altitude", 12.0)), 60.0))
            center_d = float(center_raw[2]) if len(center_raw) >= 3 else -altitude
            if center_d >= 0:
                center_d = -altitude
            center = [float(center_raw[0]), float(center_raw[1]), center_d]
            width = max(20.0, min(float(input_data.get("area_width", 80.0)), 300.0))
            height = max(20.0, min(float(input_data.get("area_height", 60.0)), 300.0))
            speed = max(2.0, min(float(input_data.get("speed", 20.0)), 40.0))
            tracks = max(2, min(int(input_data.get("tracks_per_uav", 4)), 8))
            spacing = max(
                6.0,
                min(
                    float(input_data.get("formation_spacing", input_data.get("spacing", 12.0))),
                    50.0,
                ),
            )
            formation_aliases = {
                "none": "coverage",
                "independent": "coverage",
                "parallel": "coverage",
                "triangular": "triangle",
                "wedge": "triangle",
                "\u4e09\u89d2\u5f62": "triangle",
                "\u4e09\u89d2\u9635\u5217": "triangle",
                "\u5706\u5f62": "circle",
                "\u76f4\u7ebf": "line",
            }
            requested_formation = str(input_data.get("formation") or "coverage").strip().lower()
            formation = formation_aliases.get(requested_formation, requested_formation)
            if formation not in {"coverage", "triangle", "circle", "line", "v", "vee"}:
                raise ValueError(f"Unsupported search formation: {requested_formation}")
            if formation == "vee":
                formation = "v"

            assigned_ids = sorted(
                robot_ids,
                key=lambda robot_id: (
                    float(snapshot[robot_id]["position"][1]),
                    float(snapshot[robot_id]["position"][0]),
                    _robot_sort_key(robot_id),
                ),
            )
            center_path = []
            offsets = []
            if formation == "coverage":
                search_paths = build_area_search_paths(assigned_ids, center, width, height, tracks)
            else:
                search_paths, center_path, offsets = build_formation_search_paths(
                    assigned_ids,
                    center,
                    width,
                    height,
                    tracks,
                    formation,
                    spacing,
                )

            initial_positions = {
                robot_id: tuple(snapshot[robot_id]["position"])
                for robot_id in robot_ids
            }
            max_distance = max(
                math.dist(initial_positions[robot_id], search_paths[robot_id][0])
                + _polyline_length(search_paths[robot_id])
                for robot_id in robot_ids
            )
            requested_duration = input_data.get("visual_duration")
            if requested_duration is None:
                visual_duration = max(4.0, min(max_distance / speed, 14.0))
            else:
                visual_duration = max(0.1, min(float(requested_duration), 30.0))

            frame_rate = 10.0
            previous = dict(initial_positions)
            minimum_observed = minimum_separation(previous.values())
            max_formation_error = 0.0

            def apply_frame(current, frame_seconds, moving=True):
                nonlocal previous, minimum_observed
                safe_seconds = max(frame_seconds, 1e-6)
                for robot_id in robot_ids:
                    point = current[robot_id]
                    velocity = [
                        (point[axis] - previous[robot_id][axis]) / safe_seconds
                        for axis in range(3)
                    ]
                    set_position(
                        robot_id,
                        *point,
                        velocity=velocity,
                        moving=moving,
                        in_air=True,
                    )
                minimum_observed = min(
                    minimum_observed,
                    minimum_separation(current.values()),
                )
                previous = current

            def animate_targets(targets, duration):
                frames = max(1, int(math.ceil(duration * frame_rate)))
                frame_seconds = duration / frames
                starts = dict(previous)
                for frame in range(1, frames + 1):
                    progress = frame / frames
                    current = {
                        robot_id: tuple(
                            starts[robot_id][axis]
                            + (targets[robot_id][axis] - starts[robot_id][axis]) * progress
                            for axis in range(3)
                        )
                        for robot_id in robot_ids
                    }
                    apply_frame(current, frame_seconds)
                    if frame_seconds > 0:
                        time.sleep(frame_seconds)

            if formation == "coverage":
                frames = max(2, int(math.ceil(visual_duration * frame_rate)))
                frame_seconds = visual_duration / frames
                full_paths = {
                    robot_id: [initial_positions[robot_id]] + search_paths[robot_id]
                    for robot_id in robot_ids
                }
                for frame in range(1, frames + 1):
                    progress = frame / frames
                    current = {
                        robot_id: interpolate_polyline(full_paths[robot_id], progress)
                        for robot_id in robot_ids
                    }
                    apply_frame(current, frame_seconds, moving=frame < frames)
                    if frame < frames:
                        time.sleep(frame_seconds)
            else:
                first_slots = {
                    robot_id: search_paths[robot_id][0]
                    for robot_id in robot_ids
                }
                vertical_gap = max(4.0, spacing * 0.55)
                transit_base = min(
                    [center_d] + [position[2] for position in initial_positions.values()]
                ) - max(8.0, spacing)
                altitude_targets = {
                    robot_id: (
                        initial_positions[robot_id][0],
                        initial_positions[robot_id][1],
                        transit_base - index * vertical_gap,
                    )
                    for index, robot_id in enumerate(robot_ids)
                }
                lane_targets = {
                    robot_id: (
                        first_slots[robot_id][0],
                        first_slots[robot_id][1],
                        altitude_targets[robot_id][2],
                    )
                    for robot_id in robot_ids
                }
                animate_targets(altitude_targets, visual_duration * 0.08)
                animate_targets(lane_targets, visual_duration * 0.17)
                animate_targets(first_slots, visual_duration * 0.10)

                search_duration = visual_duration * 0.65
                frames = max(2, int(math.ceil(search_duration * frame_rate)))
                frame_seconds = search_duration / frames
                formation_ids = sorted(robot_ids, key=_robot_sort_key)
                offset_by_robot = {
                    robot_id: offsets[index]
                    for index, robot_id in enumerate(formation_ids)
                }
                for frame in range(1, frames + 1):
                    progress = frame / frames
                    center_point = interpolate_polyline(center_path, progress)
                    current = {
                        robot_id: interpolate_polyline(search_paths[robot_id], progress)
                        for robot_id in robot_ids
                    }
                    apply_frame(current, frame_seconds, moving=frame < frames)
                    frame_error = max(
                        math.hypot(
                            current[robot_id][0] - center_point[0] - offset_by_robot[robot_id][0],
                            current[robot_id][1] - center_point[1] - offset_by_robot[robot_id][1],
                        )
                        for robot_id in robot_ids
                    )
                    max_formation_error = max(max_formation_error, frame_error)
                    if frame < frames:
                        time.sleep(frame_seconds)

                final_minimum = minimum_separation(previous.values())
                if final_minimum < spacing - 0.05:
                    raise RuntimeError(
                        f"Formation separation verification failed: {final_minimum:.2f}m"
                    )

            final_snapshot = get_snapshot()
            formation_preserved = (
                formation != "coverage" and max_formation_error <= 0.05
            )
            formation_name_zh = {
                "triangle": "\u4e09\u89d2\u5f62",
                "circle": "\u5706\u5f62",
                "line": "\u76f4\u7ebf",
                "v": "V \u5f62",
            }.get(formation, formation)
            if formation == "coverage":
                completion_summary = (
                    f"Area search complete: {len(robot_ids)} UAVs covered "
                    f"{width * height:.0f} square meters at 100%."
                )
                completion_summary_zh = (
                    f"\u533a\u57df\u641c\u7d22\u5b8c\u6210\uff1a{len(robot_ids)} \u67b6\u65e0\u4eba\u673a\u5df2\u8986\u76d6 "
                    f"{width * height:.0f} \u5e73\u65b9\u7c73\uff0c\u8986\u76d6\u7387 100%\u3002"
                )
            else:
                completion_summary = (
                    f"Formation area search complete: {len(robot_ids)} UAVs preserved "
                    f"a {formation} formation while covering {width * height:.0f} square meters."
                )
                completion_summary_zh = (
                    f"\u7f16\u961f\u533a\u57df\u641c\u7d22\u5b8c\u6210\uff1a{len(robot_ids)} \u67b6\u65e0\u4eba\u673a\u5168\u7a0b\u4fdd\u6301 "
                    f"{formation_name_zh} \u7f16\u961f\uff0c\u5df2\u8986\u76d6 {width * height:.0f} \u5e73\u65b9\u7c73\u3002"
                )

            return SkillResult(
                success=True,
                output={
                    "robots": sorted(robot_ids, key=_robot_sort_key),
                    "area_center": [round(value, 2) for value in center],
                    "area_width_m": round(width, 2),
                    "area_height_m": round(height, 2),
                    "searched_area_m2": round(width * height, 2),
                    "coverage_percent": 100.0,
                    "formation": formation,
                    "formation_spacing_m": round(spacing, 2) if formation != "coverage" else None,
                    "formation_preserved": formation_preserved,
                    "max_formation_error_m": round(max_formation_error, 3),
                    "completion_summary": completion_summary,
                    "completion_summary_zh": completion_summary_zh,
                    "tracks_per_uav": tracks,
                    "minimum_observed_separation_m": round(minimum_observed, 2),
                    "search_paths": {
                        robot_id: [[round(value, 2) for value in point] for point in search_paths[robot_id]]
                        for robot_id in sorted(robot_ids, key=_robot_sort_key)
                    },
                    "formation_center_path": (
                        [[round(value, 2) for value in point] for point in center_path]
                        if center_path
                        else []
                    ),
                    "final_positions": {
                        robot_id: [round(value, 2) for value in final_snapshot[robot_id]["position"]]
                        for robot_id in sorted(robot_ids, key=_robot_sort_key)
                    },
                    "mock_visualization_only": True,
                },
                cost_time=round(time.time() - started, 3),
                logs=[
                    (
                        f"Mock {formation} area search complete: {len(robot_ids)} UAVs "
                        f"covered {width * height:.0f} m2"
                    )
                ],
            )
        except Exception as exc:
            for robot_id in robot_ids:
                last = get_snapshot().get(robot_id, {}).get("position")
                if last:
                    set_position(robot_id, *last, velocity=[0.0, 0.0, 0.0], moving=False, in_air=True)
            return SkillResult(
                success=False,
                error_msg=str(exc),
                cost_time=round(time.time() - started, 3),
                logs=[f"Mock area search stopped: {exc}"],
            )


class SwarmRendezvous(Skill):
    name = "swarm_rendezvous"
    description = "Gather multiple UAVs around one center using collision-safe altitude layers, then hold or rotate."
    skill_type = "hard"
    robot_type = ["UAV"]
    preconditions = []
    cost = 5.0
    input_schema = {
        "robot_ids": "UAV IDs separated by commas",
        "center_position": "[N, E, D] rendezvous center",
        "formation": "triangle | circle | line | v",
        "spacing": "minimum slot spacing in meters, at least 6",
        "speed": "flight speed in m/s",
        "post_action": "hold | orbit",
        "hold_duration": "optional fixed hover time in seconds",
        "duration": "rotating hold duration in seconds",
        "angular_speed": "formation rotation speed in degrees/s",
    }
    output_schema = {
        "final_positions": "per-UAV final positions",
        "minimum_observed_separation_m": "minimum measured separation",
    }

    def execute(self, input_data: dict) -> SkillResult:
        return execute_swarm_motion(input_data, "triangle", "hold")


class SwarmFormationHold(Skill):
    name = "swarm_formation_hold"
    description = "Arrange active UAVs into a collision-safe formation around a selected center and hold."
    skill_type = "hard"
    robot_type = ["UAV"]
    preconditions = []
    cost = 4.0
    input_schema = {
        "robot_ids": "UAV IDs separated by commas",
        "center_position": "[N, E, D] formation center",
        "formation": "triangle | circle | line | v",
        "spacing": "minimum slot spacing in meters, at least 6",
        "speed": "flight speed in m/s",
        "hold_duration": "optional fixed hover time in seconds",
    }
    output_schema = {
        "final_positions": "per-UAV final positions",
        "minimum_observed_separation_m": "minimum measured separation",
    }

    def execute(self, input_data: dict) -> SkillResult:
        return execute_swarm_motion(input_data, "v", "hold")


class SwarmOrbitHold(Skill):
    name = "swarm_orbit_hold"
    description = "Arrange active UAVs around one center and rotate the formation while maintaining separation."
    skill_type = "hard"
    robot_type = ["UAV"]
    preconditions = []
    cost = 6.0
    input_schema = {
        "robot_ids": "UAV IDs separated by commas",
        "center_position": "[N, E, D] orbit center",
        "formation": "triangle | circle | v",
        "spacing": "minimum slot spacing in meters, at least 6",
        "speed": "flight speed in m/s",
        "duration": "rotating hold duration in seconds",
        "angular_speed": "formation rotation speed in degrees/s",
    }
    output_schema = {
        "final_positions": "per-UAV final positions",
        "minimum_observed_separation_m": "minimum measured separation",
        "orbit_steps": "completed formation rotation steps",
    }

    def execute(self, input_data: dict) -> SkillResult:
        return execute_swarm_motion(input_data, "circle", "orbit")
