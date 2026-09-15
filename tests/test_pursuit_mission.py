import math
import random
import time

from adapters.mock_adapter import MockAdapter
from brain.pursuit_mission import (
    boundary_status,
    build_local_observation,
    encirclement_motion_decision,
    encirclement_slot,
    build_pursuit_initialization,
    constrain_direction_to_area,
    direction_changed,
    evaluate_pursuit,
    parse_motion_decision,
    parse_pursuit_request,
    parse_pursuit_spec,
    position_inside_area,
    pursuit_links,
)


def _spec():
    return parse_pursuit_request(
        "UAV1-3 chase UAV4",
        ["UAV_1", "UAV_2", "UAV_3", "UAV_4"],
    )


def test_llm_structured_pursuit_spec_sets_chinese_speed_semantics():
    spec = parse_pursuit_spec(
        {
            "type": "pursuit",
            "pursuers": ["UAV1", "UAV2", "UAV3"],
            "evader": "UAV4",
            "pursuer_speed_mps": 14,
            "speed_ratio_by_robot": {"UAV4": 2.0},
        },
        ["UAV_1", "UAV_2", "UAV_3", "UAV_4"],
    )

    assert spec["pursuers"] == ["UAV_1", "UAV_2", "UAV_3"]
    assert spec["evader"] == "UAV_4"
    assert spec["speed_mps_by_robot"]["UAV_4"] == 28.0
    assert spec["evader_speed_mps"] == 28.0


def test_boundary_status_warns_before_hard_edge():
    bounds = {"north_min": -20, "north_max": 30, "east_min": 10, "east_max": 70}
    status = boundary_status([28.5, 40, -8], bounds)

    assert status["status"] == "warning"
    assert status["nearest_edge"] == "north_max"
    assert status["nearest_distance_m"] == 1.5
    assert "turn inward" in status["warning"]

def test_pursuit_request_expands_uav_range_and_sets_hard_limits():
    spec = _spec()

    assert spec["pursuers"] == ["UAV_1", "UAV_2", "UAV_3"]
    assert spec["evader"] == "UAV_4"
    assert spec["participants"] == ["UAV_1", "UAV_2", "UAV_3", "UAV_4"]
    assert spec["max_world_steps"] == spec["max_rounds"] * 4
    assert spec["capture_radius_m"] > 0


def test_randomized_pursuit_start_is_separated_and_not_already_captured():
    spec = _spec()
    initialization = build_pursuit_initialization(spec, rng=random.Random(7))
    positions = {item["robot_id"]: item["position"] for item in initialization}

    assert set(positions) == set(spec["participants"])
    pair_distances = [
        math.dist(positions[first], positions[second])
        for index, first in enumerate(spec["participants"])
        for second in spec["participants"][index + 1 :]
    ]
    assert min(pair_distances) > spec["capture_radius_m"]
    assert evaluate_pursuit(positions, spec, round_index=0, world_step=0)["status"] == "running"


def test_selected_area_is_a_hard_initialization_and_motion_boundary():
    spec = _spec()
    bounds = {"north_min": -20, "north_max": 30, "east_min": 10, "east_max": 70}
    spec["area_bounds"] = bounds
    initialization = build_pursuit_initialization(spec, rng=random.Random(19))

    assert all(position_inside_area(item["position"], bounds) for item in initialization)
    direction = constrain_direction_to_area(
        [29.5, 40, -8],
        [1, 0, 0],
        speed_mps=7,
        interval_s=1.5,
        bounds=bounds,
    )
    assert direction[0] < 0


def test_mock_physics_cannot_cross_selected_area_boundary():
    adapter = MockAdapter(realtime_factor=20.0)
    bounds = {"north_min": -5, "north_max": 5, "east_min": -4, "east_max": 4}
    try:
        adapter.connect()
        adapter.seed_fleet({"UAV_1": {"position": [4.8, 0, -8], "in_air": True}})
        adapter.set_operating_bounds(bounds, ["UAV_1"])
        result = adapter.set_velocity_ned_for("UAV_1", 20, 20, 0)
        assert result.data["boundary_warning"] is True
        assert "north_max" in result.data["boundary_blocked_axes"]
        time.sleep(0.2)
        position = adapter.get_robot_snapshot()["UAV_1"]["position"]
        assert position_inside_area(position, bounds)
        assert position[0] <= bounds["north_max"]
        assert position[1] <= bounds["east_max"]
    finally:
        adapter.disconnect()


def test_local_observation_and_links_hide_out_of_range_uavs():
    spec = _spec()
    spec.update({"sensor_range_m": 25, "communication_range_m": 15})
    positions = {
        "UAV_1": [0, 0, -8],
        "UAV_2": [10, 0, -8],
        "UAV_3": [30, 0, -8],
        "UAV_4": [60, 0, -8],
    }
    velocities = {robot_id: [0, 0, 0] for robot_id in positions}

    observation = build_local_observation("UAV_1", positions, velocities, spec)
    links = pursuit_links(positions, spec["participants"], spec["communication_range_m"])

    assert set(observation["observed_uavs"]) == {"UAV_2"}
    assert observation["communicable_peers"] == ["UAV_2"]
    assert links == [("UAV_1", "UAV_2")]


def test_invalid_llm_motion_falls_back_to_local_tactical_direction():
    spec = _spec()
    positions = {
        "UAV_1": [0, 0, -8],
        "UAV_2": [-5, 8, -8],
        "UAV_3": [-5, -8, -8],
        "UAV_4": [20, 0, -8],
    }
    velocities = {robot_id: [0, 0, 0] for robot_id in positions}
    observation = build_local_observation("UAV_1", positions, velocities, spec)

    decision = parse_motion_decision("not json", observation, spec)

    assert decision["source"] == "tactical_fallback"
    assert decision["direction"][0] > 0.9
    assert not direction_changed(decision["direction"], decision["direction"])


def test_mock_pursuit_has_continuous_motion_and_reaches_capture_before_timeout():
    spec = _spec()
    spec.update({"decision_interval_s": 1.0, "max_rounds": 20, "max_world_steps": 80})
    initialization = build_pursuit_initialization(spec, rng=random.Random(11))
    initial_positions = {item["robot_id"]: item["position"] for item in initialization}
    adapter = MockAdapter(realtime_factor=20.0)
    trajectories = {robot_id: [] for robot_id in spec["participants"]}
    try:
        adapter.connect()
        adapter.seed_fleet({
            robot_id: {"position": position, "battery": 100, "in_air": True}
            for robot_id, position in initial_positions.items()
        })
        captured = False
        for round_index in range(1, spec["max_rounds"] + 1):
            fleet = adapter.get_robot_snapshot()
            positions = {robot_id: fleet[robot_id]["position"] for robot_id in spec["participants"]}
            velocities = {robot_id: fleet[robot_id]["velocity"] for robot_id in spec["participants"]}
            for robot_id in spec["participants"]:
                observation = build_local_observation(robot_id, positions, velocities, spec)
                decision = parse_motion_decision("", observation, spec)
                direction = decision["direction"]
                speed = decision["speed_mps"]
                adapter.set_velocity_ned_for(
                    robot_id,
                    direction[0] * speed,
                    direction[1] * speed,
                    0.0,
                )

            time.sleep(spec["decision_interval_s"] / adapter.realtime_factor)
            fleet = adapter.get_robot_snapshot()
            positions = {robot_id: fleet[robot_id]["position"] for robot_id in spec["participants"]}
            for robot_id in spec["participants"]:
                trajectories[robot_id].append(tuple(positions[robot_id]))
            outcome = evaluate_pursuit(
                positions,
                spec,
                round_index=round_index,
                world_step=round_index * len(spec["participants"]),
            )
            if outcome["status"] == "complete":
                captured = True
                break

        assert captured
        assert all(math.dist(initial_positions[robot_id], path[-1]) > 2 for robot_id, path in trajectories.items())
        assert all(len(set(path)) > 2 for path in trajectories.values())
        traveled = []
        for robot_id, path in trajectories.items():
            samples = [tuple(initial_positions[robot_id]), *path]
            traveled.append(sum(math.dist(first, second) for first, second in zip(samples, samples[1:])))
        assert min(traveled) / max(traveled) >= 0.75
    finally:
        for robot_id in spec["participants"]:
            adapter.stop_velocity_for(robot_id)
        adapter.disconnect()


def test_pursuit_timeout_is_distinct_from_completion():
    spec = _spec()
    positions = {
        "UAV_1": [0, 0, -8],
        "UAV_2": [0, 20, -8],
        "UAV_3": [0, -20, -8],
        "UAV_4": [50, 0, -8],
    }

    outcome = evaluate_pursuit(
        positions,
        spec,
        round_index=spec["max_rounds"],
        world_step=spec["max_world_steps"],
    )

    assert outcome["status"] == "timeout"
    assert outcome["evidence"]["closest_distance_m"] > spec["capture_radius_m"]


def test_fast_encirclement_assigns_distinct_local_slots_without_llm():
    spec = _spec()
    positions = {
        "UAV_1": [-20, 12, -8],
        "UAV_2": [-20, 0, -8],
        "UAV_3": [-20, -12, -8],
        "UAV_4": [0, 0, -8],
    }
    velocities = {robot_id: [0, 0, 0] for robot_id in positions}
    observations = {
        robot_id: build_local_observation(robot_id, positions, velocities, spec)
        for robot_id in spec["participants"]
    }

    slots = [encirclement_slot(observations[robot_id], spec) for robot_id in spec["pursuers"]]
    decisions = [
        encirclement_motion_decision(robot_id, observations[robot_id], spec)
        for robot_id in spec["pursuers"]
    ]

    assert [slot["slot_index"] for slot in slots] == [0, 1, 2]
    assert len({round(slot["slot_angle_deg"], 1) for slot in slots}) == 3
    assert all(slot["slot_radius_m"] < spec["capture_radius_m"] for slot in slots)
    assert all(decision["source"] == "fast_encirclement" for decision in decisions)
    assert len({tuple(decision["direction"][:2]) for decision in decisions}) == 3
def test_pursuit_applies_explicit_speed_ratio_per_agent():
    spec = parse_pursuit_request(
        "UAV2-4追逐UAV1 UAV1速度更快 是别的1.5倍",
        ["UAV_1", "UAV_2", "UAV_3", "UAV_4"],
    )

    assert spec["evader"] == "UAV_1"
    assert spec["pursuers"] == ["UAV_2", "UAV_3", "UAV_4"]
    assert spec["speed_mps_by_robot"]["UAV_1"] == 21.0
    assert spec["evader_speed_mps"] == 21.0

    positions = {
        "UAV_1": [0, 0, -8],
        "UAV_2": [20, 0, -8],
        "UAV_3": [0, 20, -8],
        "UAV_4": [-20, 0, -8],
    }
    velocities = {robot_id: [0, 0, 0] for robot_id in positions}
    observation = build_local_observation("UAV_1", positions, velocities, spec)
    decision = encirclement_motion_decision("UAV_1", observation, spec)

    assert decision["speed_mps"] == 21.0
def test_fast_pursuit_message_reports_hard_boundary():
    spec = _spec()
    spec["area_bounds"] = {
        "north_min": -20,
        "north_max": 30,
        "east_min": 10,
        "east_max": 70,
    }
    positions = {
        "UAV_1": [0, 0, -8],
        "UAV_2": [20, 0, -8],
        "UAV_3": [0, 20, -8],
        "UAV_4": [-20, 0, -8],
    }
    velocities = {robot_id: [0, 0, 0] for robot_id in positions}
    observation = build_local_observation("UAV_2", positions, velocities, spec)
    decision = encirclement_motion_decision("UAV_2", observation, spec)

    assert "Hard boundary N=[-20.0, 30.0], E=[10.0, 70.0]" in decision["message"]
def test_pursuit_parses_chinese_double_speed_suffix_phrase():
    spec = parse_pursuit_request(
        "UAV1-UAV3追逐UAV4 UAV4\u6709\u4e24\u500d\u901f\u5ea6",
        ["UAV_1", "UAV_2", "UAV_3", "UAV_4"],
    )

    assert spec["evader"] == "UAV_4"
    assert spec["pursuers"] == ["UAV_1", "UAV_2", "UAV_3"]
    assert spec["speed_mps_by_robot"]["UAV_4"] == 28.0
    assert spec["evader_speed_mps"] == 28.0
