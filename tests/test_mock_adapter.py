import math
import threading
import time

import pytest

from adapters.mock_adapter import MockAdapter
from adapters.mock_dynamics import PointMassDynamics


def wait_until(predicate, timeout=1.5, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def test_point_mass_integrator_applies_damping_force_speed_and_position_in_order():
    dynamics = PointMassDynamics(
        dt=0.1,
        damping=0.25,
        max_acceleration=100.0,
    )

    step = dynamics.integrate(
        position=(0.0, 0.0, 0.0),
        velocity=(2.0, 0.0, 0.0),
        action_force=(1.0, 0.0, 0.0),
        max_speed=10.0,
    )

    assert step.velocity == pytest.approx((2.05, 0.0, 0.0))
    assert step.position == pytest.approx((0.205, 0.0, 0.0))


def test_mock_adapter_basic_flight_cycle_uses_kinematic_steps():
    adapter = MockAdapter(realtime_factor=20.0)
    try:
        assert adapter.connect()
        assert adapter.is_connected()

        takeoff = adapter.takeoff(altitude=12)
        assert takeoff.success
        assert takeoff.data["physics_steps"] > 1
        assert adapter.is_armed()
        assert adapter.is_in_air()
        assert adapter.get_position().down == -12

        fly = adapter.fly_to_ned(10, 5, -12, speed=10)
        assert fly.success
        assert fly.data["physics_steps"] > 1
        pos = adapter.get_position()
        assert (pos.north, pos.east, pos.down) == (10, 5, -12)

        land = adapter.land()
        assert land.success
        assert land.data["physics_steps"] > 1
        assert not adapter.is_armed()
        assert not adapter.is_in_air()
        assert adapter.get_position().down == 0
    finally:
        adapter.disconnect()


def test_fly_to_exposes_intermediate_positions_and_respects_speed_limit():
    adapter = MockAdapter(realtime_factor=10.0)
    samples = []
    result = {}
    try:
        adapter.connect()
        adapter.set_robot_position("UAV_1", 0, 0, -10, in_air=True)

        worker = threading.Thread(
            target=lambda: result.setdefault(
                "value", adapter.fly_to_ned(18, 6, -12, speed=6.0)
            )
        )
        worker.start()
        while worker.is_alive():
            snapshot = adapter.get_robot_snapshot()["UAV_1"]
            samples.append((tuple(snapshot["position"]), tuple(snapshot["velocity"])))
            time.sleep(0.005)
        worker.join(timeout=1.0)

        assert result["value"].success
        distinct_positions = {
            tuple(round(value, 2) for value in position)
            for position, _velocity in samples
        }
        assert len(distinct_positions) >= 5
        assert max(math.dist((0.0, 0.0, 0.0), velocity) for _position, velocity in samples) <= 6.01
        assert adapter.get_position().to_list() == [18.0, 6.0, -12.0]
    finally:
        adapter.disconnect()


def test_mock_adapter_supports_accelerated_cockpit_velocity_and_braking():
    adapter = MockAdapter(realtime_factor=10.0)

    disconnected = adapter.stop_velocity()
    assert not disconnected.success

    try:
        assert adapter.connect()
        adapter.takeoff(altitude=5)
        start = adapter.get_position()

        move = adapter.set_velocity_body(2.0, -1.0, 0.5, yaw_rate=15.0)
        assert move.success
        assert move.data["velocity_body"] == [2.0, -1.0, 0.5, 15.0]
        assert wait_until(lambda: adapter.get_position().north > start.north + 0.05)

        pos = adapter.get_position()
        velocity = adapter.get_state().velocity
        assert pos.north > start.north
        assert pos.east < start.east
        assert pos.down > start.down
        assert 0.0 < math.dist((0.0, 0.0, 0.0), velocity) <= math.sqrt(5.25) + 0.01

        stop = adapter.stop_velocity()
        assert stop.success
        assert wait_until(
            lambda: math.dist((0.0, 0.0, 0.0), adapter.get_state().velocity) < 0.04
        )
    finally:
        adapter.disconnect()


def test_mock_adapter_tracks_a_complete_fleet_independently():
    adapter = MockAdapter(realtime_factor=20.0)
    try:
        adapter.connect()
        adapter.seed_fleet({
            "UAV_1": {"position": [0, 0, 0], "battery": 95},
            "UAV_2": {"position": [18, 0, 0], "battery": 90},
            "UAV_3": {"position": [18, 18, 0], "battery": 85},
        })

        adapter.set_robot_position("UAV_2", 25, 10, -12, moving=True, in_air=True)
        snapshot = adapter.get_robot_snapshot()

        assert snapshot["UAV_1"]["position"] == [0.0, 0.0, 0.0]
        assert snapshot["UAV_2"]["position"] == [25.0, 10.0, -12.0]
        assert snapshot["UAV_2"]["moving"] is True
        assert snapshot["UAV_2"]["motion_mode"] == "scripted"
        assert snapshot["UAV_3"]["battery"] == 0.85
    finally:
        adapter.disconnect()


def test_second_uav_accepts_manual_control_during_first_uav_fly_to():
    adapter = MockAdapter(realtime_factor=10.0)
    result = {}
    try:
        adapter.connect()
        adapter.seed_fleet({
            "UAV_1": {"position": [0, 0, -10], "battery": 95, "in_air": True},
            "UAV_2": {"position": [20, 0, -10], "battery": 90, "in_air": True},
        })
        adapter.set_active_robot("UAV_1")
        worker = threading.Thread(
            target=lambda: result.setdefault(
                "value", adapter.fly_to_ned(15, 0, -10, speed=5.0)
            )
        )
        worker.start()
        assert wait_until(
            lambda: adapter.get_robot_snapshot()["UAV_1"]["position"][0] > 0.05
        )

        manual = adapter.set_velocity_body_for("UAV_2", 0.0, 3.0, 0.0)
        assert manual.success
        assert wait_until(
            lambda: adapter.get_robot_snapshot()["UAV_2"]["position"][1] > 0.05
        )
        worker.join(timeout=2.0)

        assert not worker.is_alive()
        assert result["value"].success
        snapshot = adapter.get_robot_snapshot()
        assert snapshot["UAV_1"]["position"] == [15.0, 0.0, -10.0]
        assert snapshot["UAV_2"]["position"][1] > 0.05
    finally:
        adapter.disconnect()
