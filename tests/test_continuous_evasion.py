"""Regressions for a selected escape skill while the next decision is pending."""

import math

import pytest

from adapters.mock_adapter import MockAdapter
from sim.mock_tasks import MockTask


@pytest.fixture
def scene():
    adapter = MockAdapter()
    adapter._connected = True
    task = MockTask("pursuit", role_counts={"pursuer": 1, "evader": 1})
    adapter.reset_robot_pose("UAV_1", [-10, 0, -5], in_air=True)
    adapter.reset_robot_pose("UAV_2", [0, 0, -5], in_air=True)
    adapter.retain_fleet(task.roles)
    adapter.set_operating_bounds(task.bounds, task.roles)
    task.sync(adapter.get_robot_snapshot())
    task.apply("UAV_2", "evade_peer", {"target_id": "UAV_1"}, adapter)
    yield task, adapter
    adapter.disconnect()


def command(adapter):
    return adapter._robot_states["UAV_2"]["command_velocity"]


def test_escape_keeps_accelerating_past_original_waypoint(scene):
    task, adapter = scene
    for _ in range(60):
        task.refresh_motion(adapter)
        with adapter._state_lock:
            adapter._step_world_locked()
    body = adapter.get_robot_snapshot()["UAV_2"]
    assert body["position"][0] > 10  # Passed the old reflected waypoint.
    assert body["velocity"][0] > 12
    assert math.hypot(*command(adapter)[:2]) == pytest.approx(15)


def test_escape_tracks_selected_threat_without_another_decision(scene):
    task, adapter = scene
    adapter.reset_robot_pose("UAV_1", [0, -10, -5], in_air=True)
    task.refresh_motion(adapter)
    assert command(adapter)[1] > 14
    assert abs(command(adapter)[0]) < 0.1


def test_lost_threat_keeps_last_local_bearing(scene):
    task, adapter = scene
    adapter.reset_robot_pose("UAV_1", [65, 65, -5], in_air=True)
    task.refresh_motion(adapter)
    assert not task.observe("UAV_2")["neighbors"]
    assert command(adapter)[0] > 14
    assert abs(command(adapter)[1]) < 0.1


@pytest.mark.parametrize("skill", ["hold_position", "return_in_bounds"])
def test_next_skill_replaces_escape(scene, skill):
    task, adapter = scene
    task.apply("UAV_2", skill, {}, adapter)
    task.refresh_motion(adapter)
    assert command(adapter) == pytest.approx([0, 0, 0])


def test_escape_respects_boundary_shield(scene):
    task, adapter = scene
    adapter.reset_robot_pose("UAV_1", [58, 0, -5], in_air=True)
    adapter.reset_robot_pose("UAV_2", [69, 0, -5], in_air=True)
    task.refresh_motion(adapter)
    assert command(adapter)[0] <= 0
    for _ in range(120):
        task.refresh_motion(adapter)
        with adapter._state_lock:
            adapter._step_world_locked()
        body = adapter.get_robot_snapshot()["UAV_2"]
        assert all(-70 <= value <= 70 for value in body["position"][:2])
        assert math.hypot(*command(adapter)[:2]) <= 15.000001
