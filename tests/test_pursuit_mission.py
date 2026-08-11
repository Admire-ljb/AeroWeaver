import math
import random
import time

from adapters.mock_adapter import MockAdapter
from brain.pursuit_mission import (
    build_local_observation,
    build_pursuit_initialization,
    constrain_direction_to_area,
    direction_changed,
    evaluate_pursuit,
    parse_motion_decision,
    parse_pursuit_request,
    position_inside_area,
    pursuit_links,
)


def _spec():
    return parse_pursuit_request(
        "UAV1-3 chase UAV4",
        ["UAV_1", "UAV_2", "UAV_3", "UAV_4"],
    )


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
        adapter.set_velocity_ned_for("UAV_1", 20, 20, 0)
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
