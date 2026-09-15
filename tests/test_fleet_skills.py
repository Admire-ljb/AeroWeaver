import pytest

from adapters import adapter_manager
from adapters.mock_adapter import MockAdapter
from memory.world_model import WorldModel
from skills.fleet_skills import SetFleetSize, configure_fleet_resize_handler
import server


def test_set_fleet_size_calls_injected_handler():
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "adapter": "mock",
            "active_count": kwargs["count"],
            "activated": ["UAV_3"],
            "deactivated": [],
        }

    configure_fleet_resize_handler(handler)
    result = SetFleetSize().execute({
        "count": 3,
        "reason": "area coverage",
        "robot_id": "UAV_1",
    })

    assert result.success
    assert result.output["active_count"] == 3
    assert result.output["reason"] == "area coverage"
    assert calls == [{
        "count": 3,
        "reason": "area coverage",
        "robot_id": "UAV_1",
    }]


@pytest.fixture
def isolated_mock_fleet(monkeypatch, tmp_path):
    previous_adapter = adapter_manager._adapter
    previous_connection = adapter_manager._adapter_connection_str

    world = WorldModel()
    for index in range(4):
        world.register_robot(
            f"UAV_{index + 1}",
            "UAV",
            initial_position=[index * 18, 0, 0],
            battery=92 - index * 3,
        )

    adapter = MockAdapter()
    adapter.connect()
    adapter.seed_fleet(world.get_world_state()["robots"])

    monkeypatch.setenv("SIM_ADAPTER", "mock")
    monkeypatch.setattr(server, "_FLEET_STATE_PATH", str(tmp_path / "fleet.json"))
    monkeypatch.setattr(server.state, "world_model", world)
    monkeypatch.setattr(server.state, "robot_registries", {
        robot_id: server._build_robot_registry(robot_id, "UAV")[0]
        for robot_id in world.get_world_state()["robots"]
    })
    monkeypatch.setattr(server.state, "runtime", None)
    monkeypatch.setattr(server.state, "current_robot", "UAV_4")
    monkeypatch.setattr(server.state, "is_executing", False)
    monkeypatch.setattr(server.state, "executing_robots", set())
    adapter_manager._adapter = adapter
    adapter_manager._adapter_connection_str = "mock://"

    yield adapter

    adapter_manager._adapter = previous_adapter
    adapter_manager._adapter_connection_str = previous_connection


def test_mock_fleet_resize_updates_world_registries_and_adapter(isolated_mock_fleet):
    reduced = server._synchronize_mock_fleet(2)

    assert reduced["active_count"] == 2
    assert reduced["deactivated"] == ["UAV_3", "UAV_4"]
    assert set(server.state.world_model.get_world_state()["robots"]) == {"UAV_1", "UAV_2"}
    assert set(server.state.robot_registries) == {"UAV_1", "UAV_2"}
    assert set(isolated_mock_fleet.get_robot_snapshot()) == {"UAV_1", "UAV_2"}
    assert server.state.current_robot == "UAV_1"

    expanded = server._synchronize_mock_fleet(5)

    assert expanded["active_count"] == 5
    assert expanded["activated"] == ["UAV_3", "UAV_4", "UAV_5"]
    assert set(server.state.world_model.get_world_state()["robots"]) == {
        "UAV_1", "UAV_2", "UAV_3", "UAV_4", "UAV_5",
    }
    assert set(server.state.robot_registries) == {
        "UAV_1", "UAV_2", "UAV_3", "UAV_4", "UAV_5",
    }
    assert set(isolated_mock_fleet.get_robot_snapshot()) == {
        "UAV_1", "UAV_2", "UAV_3", "UAV_4", "UAV_5",
    }
    assert all(
        robot["sensor_status"] == {
            "camera": False,
            "lidar": False,
            "microphone": False,
        }
        for robot in server.state.world_model.get_world_state()["robots"].values()
    )


def test_llm_fleet_skill_may_resize_during_ai_mission(isolated_mock_fleet):
    server.state.is_executing = True
    skill = server.state.robot_registries["UAV_1"].get_skill("set_fleet_size")

    result = skill.execute({
        "count": 3,
        "reason": "three search sectors",
        "robot_id": "UAV_1",
    })

    assert result.success
    assert result.output["active_count"] == 3
    assert result.output["adapter"] == "mock"
    assert set(server.state.world_model.get_world_state()["robots"]) == {
        "UAV_1", "UAV_2", "UAV_3",
    }


def test_set_fleet_size_rejects_unsafe_count():
    configure_fleet_resize_handler(lambda **kwargs: {"ok": True})
    result = SetFleetSize().execute({"count": 11})
    assert not result.success
    assert "between 1 and 10" in result.error_msg
